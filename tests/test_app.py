from types import SimpleNamespace
import tkinter as tk
import unittest
from unittest.mock import patch

from app import App


class AppTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        with patch("app.load_settings", return_value={}):
            self.app = App(self.root)

    def tearDown(self):
        self.root.destroy()

    def test_paste_button_replaces_existing_text_and_trims_whitespace(self):
        self.app.token.insert(0, "старый текст")
        with patch.object(self.root, "clipboard_get", return_value=" 123456:abcdefghijklmnopqrstuvwxyz\r\n"):
            self.app.paste_button.invoke()
        self.assertEqual(self.app.token.get(), "123456:abcdefghijklmnopqrstuvwxyz")

    def test_ctrl_v_works_with_russian_keyboard_layout(self):
        with patch.object(self.root, "clipboard_get", return_value="123456:abcdefghijklmnopqrstuvwxyz"):
            result = self.app.control_key(SimpleNamespace(keycode=86, keysym="Cyrillic_em"))
        self.assertEqual(result, "break")
        self.assertEqual(self.app.token.get(), "123456:abcdefghijklmnopqrstuvwxyz")

    def test_empty_clipboard_does_not_crash_or_clear_field(self):
        self.app.token.insert(0, "existing")
        with patch.object(self.root, "clipboard_get", side_effect=tk.TclError()):
            self.app.paste_token()
        self.assertEqual(self.app.token.get(), "existing")
        self.assertIn("нет текста", self.app.status.get())

    def test_folder_dialog_updates_selected_path_and_cancel_keeps_it(self):
        with patch("app.filedialog.askdirectory", return_value="D:/Мои чаты"):
            self.app.choose_folder(self.app.archive_root)
        self.assertEqual(self.app.archive_root.get(), "D:/Мои чаты")
        with patch("app.filedialog.askdirectory", return_value=""):
            self.app.choose_folder(self.app.archive_root)
        self.assertEqual(self.app.archive_root.get(), "D:/Мои чаты")

    def test_running_bot_does_not_allow_editing_folders_or_token(self):
        self.app.set_controls("disabled")
        for widget in (self.app.token, self.app.paste_button, *self.app.folder_controls):
            self.assertEqual(str(widget.cget("state")), "disabled")

    def test_help_window_contains_export_and_media_instructions(self):
        self.app.show_import_help()
        self.app.help_window.withdraw()
        def find_text(widget):
            if isinstance(widget, tk.Text):
                return widget.get("1.0", "end")
            return "".join(find_text(child) for child in widget.winfo_children())
        content = find_text(self.app.help_window)
        self.assertIn("Машиночитаемый JSON", content)
        self.assertIn("result.json", content)
        self.assertIn("ВСЮ папку", content)
        self.assertIn("Импорт JSON", content)
        self.app.help_window.destroy()


if __name__ == "__main__":
    unittest.main()
