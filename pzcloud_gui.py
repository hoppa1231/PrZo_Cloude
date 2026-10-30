#!/usr/bin/env python3
"""PZ Cloud Coop window: the same actions as pzcloud.py, as buttons.

All cloud/game work runs in a worker thread; Tk is touched only from the main
thread, so the worker talks to it through a queue.
"""
from __future__ import annotations
import os
import queue
import subprocess
import threading
import traceback
import webbrowser
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

import pzcloud as core

PAD = 8


class App:
    def __init__(self, root):
        self.root = root
        self.events = queue.Queue()
        self.busy = False
        self.info = None
        core.OUTPUT = lambda message: self.events.put(('log', str(message)))
        core.ASK = self.ask_from_worker

        root.title('PZ Cloud Coop')
        root.minsize(620, 560)
        style = ttk.Style()
        if 'clam' in style.theme_names() and os.name != 'nt':
            style.theme_use('clam')
        style.configure('Big.TButton', font=('TkDefaultFont', 12, 'bold'), padding=10)
        style.configure('Title.TLabel', font=('TkDefaultFont', 15, 'bold'))
        style.configure('Value.TLabel', font=('TkDefaultFont', 10, 'bold'))

        top = ttk.Frame(root, padding=PAD)
        top.pack(fill='x')
        ttk.Label(top, text='PZ Cloud Coop', style='Title.TLabel').pack(side='left')
        ttk.Label(top, text='  ' + core.VERSION, foreground='gray').pack(side='left')

        box = ttk.LabelFrame(root, text='Мир', padding=PAD)
        box.pack(fill='x', padx=PAD)
        self.fields = {}
        rows = [('cloud', 'Облако'), ('version', 'Версия мира'), ('last', 'Последний хост'),
                ('busy', 'Сейчас играет'), ('token', 'Токен Яндекса')]
        for i, (key, label) in enumerate(rows):
            ttk.Label(box, text=label + ':').grid(row=i, column=0, sticky='w', pady=1)
            value = ttk.Label(box, text='…', style='Value.TLabel')
            value.grid(row=i, column=1, sticky='w', padx=PAD)
            self.fields[key] = value

        main = ttk.Frame(root, padding=(PAD, PAD, PAD, 0))
        main.pack(fill='x')
        self.play = ttk.Button(main, text='▶  Играть — стать хостом', style='Big.TButton', command=self.on_play)
        self.play.pack(side='left', expand=True, fill='x', padx=(0, 4))
        self.upload = ttk.Button(main, text='⬆  Я закончил — выгрузить мир', style='Big.TButton',
                                 command=lambda: self.run(core.finish))
        self.upload.pack(side='left', expand=True, fill='x', padx=(4, 0))

        row = ttk.Frame(root, padding=(PAD, 4))
        row.pack(fill='x')
        self.buttons = [self.play, self.upload]
        for text, cmd in (('Подключить Яндекс Диск', self.connect_dialog), ('Настройки', self.settings_dialog),
                          ('Обновить статус', self.refresh)):
            b = ttk.Button(row, text=text, command=cmd)
            b.pack(side='left', padx=(0, 4))
            self.buttons.append(b)

        extra = ttk.LabelFrame(root, text='Дополнительно', padding=(PAD, 4))
        extra.pack(fill='x', padx=PAD)
        for text, cmd in (('Первая публикация мира', lambda: self.run(core.bootstrap)),
                          ('Отменить сессию', lambda: self.run(core.cancel)),
                          ('Снять зависшую блокировку', lambda: self.run(core.force_unlock)),
                          ('Папка бэкапов', self.open_backups)):
            b = ttk.Button(extra, text=text, command=cmd)
            b.pack(side='left', padx=(0, 4))
            self.buttons.append(b)

        self.log = scrolledtext.ScrolledText(root, height=12, state='disabled', wrap='word')
        self.log.pack(fill='both', expand=True, padx=PAD, pady=PAD)
        self.log.tag_configure('err', foreground='#c0392b')
        self.log.tag_configure('ok', foreground='#1e8449')

        root.protocol('WM_DELETE_WINDOW', self.on_close)
        root.after(100, self.pump)
        root.after(300, self.first_run)

    # ---- worker plumbing ------------------------------------------------------
    def ask_from_worker(self, message):
        answer, done = [False], threading.Event()
        self.events.put(('ask', message, answer, done))
        done.wait()
        return answer[0]

    def run(self, task, on_success=None, refresh=True):
        if self.busy:
            messagebox.showinfo('Подождите', 'Уже выполняется другая операция.')
            return
        self.set_busy(True)

        def work():
            ok = False
            try:
                task()
                ok = True
            except core.ERRORS as exc:
                self.events.put(('error', str(exc)))
            except Exception:
                self.events.put(('error', traceback.format_exc()))
            self.events.put(('done', ok, on_success, refresh))

        threading.Thread(target=work, daemon=True).start()

    def pump(self):
        try:
            while True:
                kind, *rest = self.events.get_nowait()
                if kind == 'log':
                    self.write(rest[0])
                elif kind == 'ask':
                    message, answer, done = rest
                    answer[0] = messagebox.askyesno('Подтверждение', message, icon='warning')
                    done.set()
                elif kind == 'error':
                    self.write('ОШИБКА: ' + rest[0], 'err')
                    messagebox.showerror('Ошибка', rest[0][-1200:])
                elif kind == 'done':
                    ok, on_success, refresh = rest
                    self.set_busy(False)
                    if ok and on_success:
                        on_success()
                    if refresh:
                        self.refresh()
                elif kind == 'status':
                    self.show_status(rest[0])
        except queue.Empty:
            pass
        self.root.after(100, self.pump)

    def write(self, text, tag=None):
        if tag is None and (text.startswith('ГОТОВО') or 'подключён' in text or 'опубликован' in text):
            tag = 'ok'
        self.log.configure(state='normal')
        self.log.insert('end', text + '\n', tag or ())
        self.log.see('end')
        self.log.configure(state='disabled')

    def set_busy(self, busy):
        self.busy = busy
        for b in self.buttons:
            b.state(['disabled'] if busy else ['!disabled'])
        self.root.configure(cursor='watch' if busy else '')
        if not busy and self.info:
            self.show_status(self.info)

    # ---- status ---------------------------------------------------------------
    def refresh(self):
        if not core.CONFIG_FILE.is_file():
            self.fields['cloud'].configure(text='не настроено — нажмите «Настройки»')
            return

        def work():
            try:
                self.events.put(('status', core.status_info()))
            except core.ERRORS as exc:
                self.events.put(('log', 'Статус недоступен: ' + str(exc)))

        threading.Thread(target=work, daemon=True).start()

    def show_status(self, s):
        self.info = s
        f = self.fields
        f['cloud'].configure(text=f"{s['remote']}  (сервер {s['server']}, вы — {s['user']})")
        f['version'].configure(text=s['version'] or 'ещё не опубликован — нужна «Первая публикация»')
        f['last'].configure(text=f"{s['last_host']}, {fmt_time(s['last_time'])}" if s['last_host'] else '—')
        if s['mine']:
            f['busy'].configure(text='ВЫ (сессия открыта — после игры выгрузите мир)', foreground='#b9770e')
        elif s['busy']:
            f['busy'].configure(text=f"{s['busy']} с {fmt_time(s['busy_since'])}", foreground='#c0392b')
        else:
            f['busy'].configure(text='никто — мир свободен', foreground='#1e8449')
        days = s['token_days']
        if days is None:
            f['token'].configure(text='общая настройка rclone', foreground='')
        else:
            f['token'].configure(text=f'ещё {days} дн.' + ('  — скоро обновить!' if days < 30 else ''),
                                 foreground='#c0392b' if days < 30 else '')
        if not self.busy:
            can_play = bool(s['version']) and not s['busy'] and not s['session']
            self.play.state(['!disabled'] if can_play else ['disabled'])
            self.upload.state(['!disabled'] if s['session'] else ['disabled'])

    # ---- actions --------------------------------------------------------------
    def on_play(self):
        if not messagebox.askyesno('Играть хостом',
                                   'Вы договорились в чате, что сегодня хост — вы?\n\n'
                                   'Программа скачает мир, откроет Project Zomboid и сама выгрузит мир, '
                                   'когда вы полностью выйдете из игры. Не закрывайте это окно до «ГОТОВО».'):
            return
        self.run(core.begin)

    def open_backups(self):
        core.LOCAL_BACKUPS.mkdir(parents=True, exist_ok=True)
        open_path(core.LOCAL_BACKUPS)

    def on_close(self):
        if self.busy and not messagebox.askyesno(
                'Операция не завершена',
                'Идёт работа с миром. Если закрыть окно сейчас, мир НЕ выгрузится автоматически — '
                'потом откройте программу и нажмите «Я закончил — выгрузить мир».\n\nВсё равно закрыть?'):
            return
        self.root.destroy()

    def first_run(self):
        if not core.RCLONE_CONF.is_file() and not global_remote_exists():
            self.write('Шаг 1: подключите свой Яндекс Диск.')
            self.connect_dialog(then=self.first_run)
        elif not core.CONFIG_FILE.is_file():
            self.write('Шаг 2: заполните настройки.')
            self.settings_dialog()
        else:
            self.refresh()

    # ---- dialogs --------------------------------------------------------------
    def connect_dialog(self, then=None):
        win = dialog(self.root, 'Подключить Яндекс Диск')
        text = ('1. Нажмите «Открыть Яндекс». Войдите в СВОЙ аккаунт и нажмите «Разрешить».\n'
                '2. Яндекс покажет токен — скопируйте его (или весь адрес страницы) и вставьте ниже.\n'
                '3. Нажмите «Подключить». Если rclone ещё не установлен, программа скачает его сама.')
        ttk.Label(win, text=text, wraplength=480, justify='left').grid(row=0, column=0, columnspan=2, sticky='w')
        ttk.Label(win, text='ClientID приложения:').grid(row=1, column=0, sticky='w', pady=(PAD, 0))
        cid = ttk.Entry(win, width=40)
        cid.insert(0, core.default_client_id())
        cid.grid(row=1, column=1, sticky='we', pady=(PAD, 0))
        ttk.Button(win, text='Открыть Яндекс', command=lambda: webbrowser.open(core.auth_url(cid.get().strip()))) \
            .grid(row=2, column=0, columnspan=2, sticky='we', pady=PAD)
        ttk.Label(win, text='Токен или адрес:').grid(row=3, column=0, sticky='w')
        token = ttk.Entry(win, width=40, show='•')
        token.grid(row=3, column=1, sticky='we')

        def done():
            win.master.destroy()
            if then:
                then()
            else:
                self.refresh()

        ttk.Button(win, text='Подключить', style='Big.TButton',
                   command=lambda: self.run(lambda: core.save_token(cid.get(), token.get()), on_success=done,
                                            refresh=False)) \
            .grid(row=4, column=0, columnspan=2, sticky='we', pady=(PAD, 0))
        win.columnconfigure(1, weight=1)

    def settings_dialog(self):
        win = dialog(self.root, 'Настройки')
        values = core.setup_defaults()
        entries = {}
        rows = [('user', 'Ваше имя'), ('server', 'Имя сервера (как в меню «Хост»)'),
                ('remote', 'Папка в облаке'), ('zomboid_dir', 'Папка Zomboid')]
        for i, (key, label) in enumerate(rows):
            ttk.Label(win, text=label + ':').grid(row=i, column=0, sticky='w', pady=2)
            e = ttk.Entry(win, width=44)
            e.insert(0, values.get(key, ''))
            e.grid(row=i, column=1, sticky='we', pady=2)
            entries[key] = e

        def browse():
            path = filedialog.askdirectory(parent=win, initialdir=entries['zomboid_dir'].get() or str(core.HERE))
            if path:
                entries['zomboid_dir'].delete(0, 'end')
                entries['zomboid_dir'].insert(0, path)

        ttk.Button(win, text='…', width=3, command=browse).grid(row=3, column=2, padx=(4, 0))
        hint = ('Имя сервера и папка в облаке должны быть одинаковыми у всех друзей. '
                'Папка Zomboid — там, где лежат Saves и Server (на Linux с Proton — внутри compatdata/108600).')
        ttk.Label(win, text=hint, wraplength=480, foreground='gray', justify='left') \
            .grid(row=4, column=0, columnspan=3, sticky='w', pady=PAD)

        def save():
            args = [entries[k].get() for k in ('user', 'server', 'remote', 'zomboid_dir')]
            self.run(lambda: core.save_setup(*args), on_success=win.master.destroy)

        ttk.Button(win, text='Сохранить', style='Big.TButton', command=save) \
            .grid(row=5, column=0, columnspan=3, sticky='we')
        win.columnconfigure(1, weight=1)


def dialog(parent, title):
    win = tk.Toplevel(parent)
    win.title(title)
    win.transient(parent)
    win.resizable(False, False)
    frame = ttk.Frame(win, padding=PAD * 2)
    frame.pack(fill='both', expand=True)
    return frame


def global_remote_exists():
    exe = core.rclone_exe()
    if not exe:
        return False
    try:
        p = subprocess.run([exe, 'listremotes'], capture_output=True, text=True, timeout=15, **core.NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return (core.REMOTE_NAME + ':') in p.stdout.split()


def fmt_time(iso):
    if not iso:
        return '?'
    try:
        return core.dt.datetime.fromisoformat(iso).astimezone().strftime('%d.%m %H:%M')
    except ValueError:
        return iso


def open_path(path):
    if os.name == 'nt':
        os.startfile(path)
    else:
        subprocess.Popen(['xdg-open', str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == '__main__':
    main()
