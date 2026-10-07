"""Minimal Windows launcher for the Dialog Spy Business archive."""
import json
import os
from pathlib import Path
import queue
import re
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog

from archive import Archive
from telegram_api import ApiError, Telegram
from storage import app_folder, load_settings, prepare_storage
from export_import import ImportCancelled, import_export
from import_help import IMPORT_INSTRUCTIONS

ALLOWED = ["message", "business_connection", "business_message",
           "edited_business_message", "deleted_business_messages"]


def run_bot(token, stop, events, archive_root=None, media_root=""):
    archive = None
    lock_file = None
    locked = False
    def report(text):
        events.put(("status", text))
    try:
        api = Telegram(token)
        me = api.call("getMe")
        if not me.get("can_connect_to_business"):
            report("Включите Business Mode для бота в @BotFather.")
            return
        # The per-bot lock always stays in AppData, even when archives use another drive.
        folder = app_folder() / str(me["id"])
        folder.mkdir(parents=True, exist_ok=True)
        import msvcrt
        lock_file = (folder / "running.lock").open("a+b")
        if lock_file.tell() == 0:
            lock_file.write(b"0")
            lock_file.flush()
        lock_file.seek(0)
        try:
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            locked = True
        except OSError:
            report("Этот бот уже запущен в другом окне.")
            return
        # Refuse an existing webhook rather than disabling a running deployment.
        if api.call("getWebhookInfo").get("url"):
            report("Токен уже используется через webhook. Нужен отдельный бот.")
            return
        try:
            report("Подготовка папок архива…")
            database, media_folder = prepare_storage(me["id"], archive_root or app_folder(), media_root)
            archive = Archive(database, api, media_folder=media_folder)
        except (OSError, ValueError) as exc:
            report(str(exc) if isinstance(exc, ValueError) else "Не удалось записать архив. Проверьте выбранные папки и свободное место.")
            return
        last_refresh = 0
        def status():
            rows = archive.db.execute("SELECT data FROM connections").fetchall()
            active = any(json.loads(row[0])["is_enabled"] for row in rows)
            report("Бот запущен" if active else "Бот запущен. Привяжите бота к Telegram Business.")
        status()
        while not stop.is_set():
            try:
                if time.monotonic() - last_refresh > 60:
                    for row in archive.db.execute("SELECT id FROM connections").fetchall():
                        connection = api.call("getBusinessConnection", business_connection_id=row[0])
                        with archive.db:
                            archive.db.execute("UPDATE connections SET data=? WHERE id=?", (json.dumps(connection), row[0]))
                    last_refresh = time.monotonic()
                # Polling continues even if sending a notification is temporarily blocked.
                updates = api.call("getUpdates", offset=archive.offset,
                                   timeout=10, allowed_updates=ALLOWED)
                for update in updates:
                    if stop.is_set():
                        break
                    archive.process(update)
                status()
                archive.deliver(stop, report)
            except ApiError as exc:
                if exc.code in (401, 404):
                    report("Неверный или отозванный токен.")
                    return
                if exc.code == 409:
                    report("Токен уже используется другим запущенным ботом.")
                    return
                report("Нет связи с Telegram. Подключение восстановится автоматически.")
                stop.wait(max(3, exc.retry_after))
    except ApiError as exc:
        report("Неверный или отозванный токен." if exc.code in (401, 404)
               else "Не удалось подключиться к Telegram. Проверьте интернет и повторите запуск.")
    except Exception:
        report("Ошибка работы. Проверьте доступ к выбранным папкам и свободное место.")
    finally:
        if archive:
            archive.db.close()
        if lock_file:
            if locked:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            lock_file.close()
        events.put(("finished", None))


def run_import(bot_id, source, archive_root, media_root, stop, events):
    archive, lock_file, locked = None, None, False
    try:
        import msvcrt
        folder = app_folder() / str(bot_id)
        folder.mkdir(parents=True, exist_ok=True)
        lock_file = (folder / "running.lock").open("a+b")
        if lock_file.tell() == 0:
            lock_file.write(b"0")
            lock_file.flush()
        lock_file.seek(0)
        try:
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            locked = True
        except OSError:
            events.put(("import_status", "Этот бот уже запущен. Закройте его окно перед импортом."))
            return
        database, media = prepare_storage(bot_id, archive_root, media_root)
        # Offline: the token only selects the correct bot's folder, no Telegram calls.
        archive = Archive(database, None, media_folder=media)
        result = import_export(source, archive, stop, lambda text: events.put(("import_status", text)))
        events.put(("import_status", f"Импорт завершён: {result['chats']} чатов, {result['messages']} сообщений, "
                    f"{result['files']} файлов. Недоступно файлов: {result['missing_files']}."))
        events.put(("import_folder", result["folder"]))
    except ImportCancelled:
        events.put(("import_status", "Импорт отменён."))
    except OSError:
        events.put(("import_status", "Не удалось скопировать экспорт. Проверьте папки, права и свободное место."))
    except ValueError as exc:
        # Do not display parser diagnostics containing private chat contents.
        from ijson.common import JSONError
        events.put(("import_status", "Неверный или незавершённый JSON. Повторите экспорт в Telegram Desktop."
                    if isinstance(exc, JSONError) else str(exc)))
    except Exception:
        events.put(("import_status", "Не удалось прочитать экспорт. Выберите готовый result.json из Telegram Desktop."))
    finally:
        if archive:
            archive.db.close()
        if lock_file:
            if locked:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            lock_file.close()
        events.put(("import_finished", None))


class App:
    def __init__(self, root):
        self.root = root
        self.events = queue.Queue()
        self.stop = threading.Event()
        self.thread = None
        self.import_thread = None
        self.import_stop = threading.Event()
        self.import_folder = None
        self.help_window = None
        self.closing = False
        settings = load_settings()
        self.archive_root = tk.StringVar(value=settings.get("archive_root", str(app_folder())))
        self.media_root = tk.StringVar(value=settings.get("media_root", ""))
        root.title("Кошка бот")
        root.geometry("680x620")
        root.resizable(False, False)
        frame = ttk.Frame(root, padding=24)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="Вставьте токен бота", font=("Segoe UI", 13)).pack(anchor="w")
        token_row = ttk.Frame(frame)
        token_row.pack(fill="x", pady=(12, 18))
        self.token = ttk.Entry(token_row, show="•", font=("Segoe UI", 11))
        self.token.pack(side="left", fill="x", expand=True)
        self.paste_button = ttk.Button(token_row, text="Вставить", command=self.paste_token)
        self.paste_button.pack(side="left", padx=(10, 0))
        # Windows key codes work with Russian and English keyboard layouts alike.
        self.token.bind("<Control-KeyPress>", self.control_key)
        self.token.bind("<Shift-Insert>", self.paste_token)
        self.token.bind("<<Paste>>", self.paste_token)
        self.token_menu = tk.Menu(root, tearoff=False)
        self.token_menu.add_command(label="Вставить", command=self.paste_token)
        self.token_menu.add_command(label="Выделить всё", command=self.select_token)
        self.token.bind("<Button-3>", self.show_token_menu)
        self.token.focus_set()
        self.folder_controls = []
        self.folder_row(frame, "Папка архива чатов", self.archive_root)
        self.folder_row(frame, "Папка для фото, GIF и других файлов", self.media_root)
        ttk.Label(frame, text="Если папка медиа не указана, файлы сохраняются рядом с архивом.",
                  foreground="#666666").pack(anchor="w", pady=(0, 18))
        import_row = ttk.Frame(frame)
        import_row.pack(fill="x")
        self.import_button = ttk.Button(import_row, text="Импорт JSON…", command=self.choose_import)
        self.import_button.pack(side="left")
        ttk.Button(import_row, text="Как получить JSON?", command=self.show_import_help).pack(side="left", padx=10)
        self.cancel_import_button = ttk.Button(import_row, text="Отменить импорт", command=self.import_stop.set, state="disabled")
        self.cancel_import_button.pack(side="left")
        self.import_status = tk.StringVar(value="Импорт старых чатов из Telegram Desktop — до запуска бота.")
        ttk.Label(frame, textvariable=self.import_status, wraplength=630).pack(anchor="w", pady=(10, 4))
        self.progress = ttk.Progressbar(frame, mode="indeterminate")
        self.progress.pack(fill="x", pady=(4, 8))
        self.open_import_button = ttk.Button(frame, text="Открыть импортированные чаты", command=self.open_import_folder, state="disabled")
        self.open_import_button.pack(anchor="w", pady=(0, 16))
        self.button = ttk.Button(frame, text="Запустить", command=self.start)
        self.button.pack(anchor="w")
        self.status = tk.StringVar(value="")
        ttk.Label(frame, textvariable=self.status, wraplength=630).pack(anchor="w", pady=(18, 0))
        ttk.Label(frame, text="Программа работает, пока это окно открыто.", foreground="#666666").pack(anchor="w", pady=(8, 0))
        root.bind("<Return>", lambda _: self.start())
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(100, self.poll)

    def folder_row(self, frame, label, variable):
        ttk.Label(frame, text=label).pack(anchor="w")
        row = ttk.Frame(frame)
        row.pack(fill="x", pady=(6, 14))
        entry = ttk.Entry(row, textvariable=variable)
        entry.pack(side="left", fill="x", expand=True)
        button = ttk.Button(row, text="Выбрать…", command=lambda: self.choose_folder(variable))
        button.pack(side="left", padx=(10, 0))
        self.folder_controls.extend((entry, button))

    def choose_folder(self, variable):
        current = Path(variable.get()).expanduser() if variable.get() else Path(self.archive_root.get()).expanduser()
        selected = filedialog.askdirectory(parent=self.root, title="Выберите папку сохранения",
                                            initialdir=str(current) if current.is_dir() else str(Path.home()))
        if selected:
            variable.set(selected)

    def select_token(self, event=None):
        self.token.selection_range(0, "end")
        self.token.icursor("end")
        return "break"

    def control_key(self, event):
        if event.keycode == 86 or event.keysym.lower() in ("v", "cyrillic_em"):
            return self.paste_token(event)
        if event.keycode == 65 or event.keysym.lower() in ("a", "cyrillic_ef"):
            return self.select_token(event)

    def paste_token(self, event=None):
        if str(self.token.cget("state")) == "disabled":
            return "break"
        try:
            text = self.root.clipboard_get().strip()
        except tk.TclError:
            self.status.set("В буфере обмена нет текста. Скопируйте токен из @BotFather.")
            return "break"
        # This is a token-only field: replace its contents, including accidental newlines.
        self.token.delete(0, "end")
        self.token.insert(0, text)
        self.token.focus_set()
        return "break"

    def show_token_menu(self, event):
        if str(self.token.cget("state")) != "disabled":
            try:
                self.token_menu.tk_popup(event.x_root, event.y_root)
            finally:
                self.token_menu.grab_release()
        return "break"

    def set_controls(self, state):
        for widget in (self.token, self.paste_button, self.button, self.import_button, *self.folder_controls):
            widget.configure(state=state)

    def show_import_help(self):
        if self.help_window and self.help_window.winfo_exists():
            self.help_window.lift()
            return
        window = self.help_window = tk.Toplevel(self.root)
        window.title("Как экспортировать и импортировать чаты")
        window.geometry("760x640")
        window.minsize(580, 420)
        frame = ttk.Frame(window, padding=16)
        frame.pack(fill="both", expand=True)
        scrollbar = ttk.Scrollbar(frame)
        scrollbar.pack(side="right", fill="y")
        text = tk.Text(frame, wrap="word", font=("Segoe UI", 11), padx=12, pady=12,
                       yscrollcommand=scrollbar.set)
        text.pack(fill="both", expand=True)
        scrollbar.configure(command=text.yview)
        text.insert("1.0", IMPORT_INSTRUCTIONS)
        text.configure(state="disabled")
        ttk.Button(window, text="Закрыть", command=window.destroy).pack(pady=(0, 14))

    def choose_import(self):
        if (self.thread and self.thread.is_alive()) or (self.import_thread and self.import_thread.is_alive()):
            return
        token = self.token.get().strip()
        if not re.fullmatch(r"\d+:[A-Za-z0-9_-]{20,}", token):
            self.import_status.set("Сначала вставьте токен бота: его ID определяет папку архива.")
            return
        if not self.archive_root.get().strip():
            self.import_status.set("Выберите папку архива чатов.")
            return
        source = filedialog.askopenfilename(parent=self.root, title="Выберите JSON-экспорт Telegram Desktop",
                                            filetypes=[("Telegram JSON", "*.json")])
        if not source:
            return
        self.import_stop.clear()
        self.set_controls("disabled")
        self.cancel_import_button.configure(state="normal")
        self.import_status.set("Подготовка импорта…")
        self.progress.start(12)
        self.import_thread = threading.Thread(target=run_import,
            args=(int(token.split(":", 1)[0]), source, self.archive_root.get().strip(),
                  self.media_root.get().strip(), self.import_stop, self.events), daemon=True)
        self.import_thread.start()

    def open_import_folder(self):
        if self.import_folder and Path(self.import_folder).is_dir():
            try:
                os.startfile(self.import_folder)
            except OSError:
                self.import_status.set("Не удалось открыть папку. Она находится в архиве → ID бота → exports.")

    def start(self):
        if (self.thread and self.thread.is_alive()) or (self.import_thread and self.import_thread.is_alive()):
            return
        token = self.token.get().strip()
        if not re.fullmatch(r"\d+:[A-Za-z0-9_-]{20,}", token):
            self.status.set("Вставьте токен, который выдал @BotFather.")
            return
        archive_root = self.archive_root.get().strip()
        if not archive_root:
            self.status.set("Выберите папку для архива чатов.")
            return
        self.stop.clear()
        self.set_controls("disabled")
        self.status.set("Подключение к Telegram…")
        self.thread = threading.Thread(target=run_bot,
                                       args=(token, self.stop, self.events, archive_root, self.media_root.get().strip()), daemon=True)
        self.thread.start()

    def poll(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "status" and not self.closing:
                    self.status.set(value)
                elif kind == "finished":
                    self.set_controls("normal")
                elif kind == "import_status" and not self.closing:
                    self.import_status.set(value)
                elif kind == "import_folder":
                    self.import_folder = value
                    self.open_import_button.configure(state="normal")
                elif kind == "import_finished":
                    self.progress.stop()
                    self.cancel_import_button.configure(state="disabled")
                    self.set_controls("normal")
        except queue.Empty:
            pass
        if self.closing and (not self.thread or not self.thread.is_alive()) and (not self.import_thread or not self.import_thread.is_alive()):
            self.root.destroy()
            return
        self.root.after(100, self.poll)

    def close(self):
        self.closing = True
        self.stop.set()
        self.import_stop.set()
        self.status.set("Завершение работы…")
        self.root.withdraw()


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
