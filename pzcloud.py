#!/usr/bin/env python3
"""PZ Cloud Coop: portable, conservative save hand-off via rclone/Yandex Disk.

Each player's Yandex token lives only in their own rclone.conf next to the app.
This is best-effort coordination, NOT an atomic distributed lock.
"""
from __future__ import annotations
import argparse
import datetime as dt
import hashlib
import json
import os
import pathlib
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import uuid
import webbrowser
import zipfile

VERSION = '0.4.0-preview'
# A PyInstaller build unpacks code into a temp dir; data must live next to the .exe.
HERE = (pathlib.Path(sys.executable) if getattr(sys, 'frozen', False) else pathlib.Path(__file__)).resolve().parent
CONFIG_FILE = HERE / 'pzcloud-config.json'
DEFAULTS_FILE = HERE / 'pzcloud-defaults.json'
STATE_FILE = HERE / 'pzcloud-session.json'
LOCAL_BACKUPS = HERE / 'local-backups'
RCLONE_CONF = HERE / 'rclone.conf'
RCLONE_DIR = HERE / 'rclone-bin'
REMOTE_NAME = 'yadisk'
SAFE = re.compile(r'^[A-Za-z0-9_.-]{1,100}$')
GAME_ID = '108600'
# Keeps rclone/powershell from flashing console windows under the Windows GUI build.
NO_WINDOW = {'creationflags': 0x08000000} if os.name == 'nt' else {}


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')


def console_ask(message):
    return input(message + ' Введите ДА: ').strip() == 'ДА'


# The GUI swaps these to route output into its log and questions into dialogs.
OUTPUT = lambda message: print(message, flush=True)
ASK = console_ask


def say(message):
    OUTPUT(message)


def die(message):
    raise RuntimeError(message)


def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.new')
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    temp.replace(path)


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def config():
    if not CONFIG_FILE.is_file():
        die('Нет настроек. Сначала откройте «Настройки» (пункт 1).')
    data = read_json(CONFIG_FILE)
    if not SAFE.fullmatch(data['server']):
        die('Небезопасное имя сервера. Используйте латиницу, цифры, дефис и _.')
    if not data['remote'].count(':') == 1 or not data['remote'].split(':', 1)[0]:
        die('Некорректный rclone remote (пример: yadisk:PZ-Shared).')
    return data


def root_dir(cfg):
    return pathlib.Path(cfg['zomboid_dir']).expanduser().resolve()


def work_tmp(cfg, prefix):
    # Big worlds must not go to /tmp: on many Linux systems it is RAM (tmpfs).
    # Staging inside the Zomboid folder also keeps the final move on one disk.
    base = root_dir(cfg) / '.pzcloud-tmp'
    base.mkdir(parents=True, exist_ok=True)
    return tempfile.TemporaryDirectory(prefix=prefix, dir=base)


def remote(cfg, leaf=''):
    return cfg['remote'].rstrip('/') + ('/' + leaf.lstrip('/') if leaf else '')


def rclone_exe():
    local = RCLONE_DIR / ('rclone.exe' if os.name == 'nt' else 'rclone')
    if local.is_file():
        return str(local)
    return shutil.which('rclone')


def rc(*args, capture=False, allow_failure=False):
    exe = rclone_exe()
    if not exe:
        die('rclone не найден. Нажмите «Подключить Яндекс Диск» — программа скачает его сама.')
    call = [exe, *[str(x) for x in args], '--retries', '3', '--low-level-retries', '3']
    # Without our own rclone.conf, fall back to the player's global rclone config.
    if RCLONE_CONF.is_file():
        call += ['--config', str(RCLONE_CONF)]
    kw = {'text': True, 'encoding': 'utf-8', 'errors': 'replace', **NO_WINDOW}
    if capture:
        proc = subprocess.run(call, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kw)
    else:
        # Long transfers: stream rclone's one-line progress into our log.
        say('rclone: ' + str(args[0]) + ' …')
        call += ['--stats', '5s', '--stats-one-line', '--stats-log-level', 'NOTICE']
        proc = subprocess.Popen(call, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, **kw)
        tail = []
        for line in proc.stderr:
            line = line.strip()
            if line:
                say('  ' + line)
                tail = (tail + [line])[-8:]
        proc.wait()
        proc.stderr = '\n'.join(tail)
    if proc.returncode and not allow_failure:
        detail = (proc.stderr or '').strip()[-650:]
        die(f'rclone завершился с ошибкой {proc.returncode}. {detail}')
    return proc


def fetch(url, timeout=60):
    # downloads.rclone.org answers 403 to Python's default User-Agent.
    return urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'pzcloud/' + VERSION}), timeout=timeout)


def install_rclone():
    """Download the official rclone build next to the app and verify its SHA-256."""
    system = {'win32': 'windows', 'linux': 'linux', 'darwin': 'osx'}.get(sys.platform)
    arch = {'x86_64': 'amd64', 'amd64': 'amd64', 'aarch64': 'arm64', 'arm64': 'arm64'}.get(platform.machine().lower())
    if not system or not arch:
        die('Не умею скачивать rclone для этой системы. Установите rclone вручную.')
    base = 'https://downloads.rclone.org/'
    version = fetch(base + 'version.txt').read().decode().split()[-1]
    if not re.fullmatch(r'v\d+\.\d+\.\d+', version):
        die('Не удалось узнать версию rclone: ' + version)
    name = f'rclone-{version}-{system}-{arch}.zip'
    sums = fetch(f'{base}{version}/SHA256SUMS').read().decode()
    expected = next((ln.split()[0] for ln in sums.splitlines() if ln.endswith('  ' + name)), None)
    if not expected:
        die('В SHA256SUMS нет ' + name)
    say(f'Скачиваю {name} с downloads.rclone.org …')
    RCLONE_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=RCLONE_DIR) as td:
        archive = pathlib.Path(td) / name
        with fetch(f'{base}{version}/{name}') as src, open(archive, 'wb') as out:
            shutil.copyfileobj(src, out, 2**20)
        if sha256(archive) != expected:
            die('Контрольная сумма rclone не совпала, файл не установлен.')
        binary = 'rclone.exe' if system == 'windows' else 'rclone'
        with zipfile.ZipFile(archive) as zf:
            member = next(m for m in zf.namelist() if m.endswith('/' + binary))
            target = RCLONE_DIR / binary
            with zf.open(member) as src, open(target, 'wb') as out:
                shutil.copyfileobj(src, out, 2**20)
        target.chmod(0o755)
    say('rclone установлен: ' + str(target))


# ---- Yandex login -------------------------------------------------------------
# The OAuth app (see README) only needs its ClientID: the implicit flow shows the
# token on oauth.yandex.ru/verification_code, so no client secret is shipped.

def auth_url(client_id):
    return 'https://oauth.yandex.ru/authorize?' + urllib.parse.urlencode(
        {'response_type': 'token', 'client_id': client_id, 'force_confirm': 'yes'})


def parse_token(text):
    """Accept either the bare token or the whole verification_code URL."""
    text = text.strip()
    fields = urllib.parse.parse_qs(text.split('#', 1)[1]) if '#' in text else {}
    token = (fields.get('access_token') or [text])[0].strip()
    if not re.fullmatch(r'[A-Za-z0-9_.\-]{20,}', token):
        die('Это не похоже на токен Яндекса. Скопируйте его со страницы целиком.')
    expires = int((fields.get('expires_in') or ['31536000'])[0])
    return token, expires


def read_rclone_conf():
    if not RCLONE_CONF.is_file():
        return {}
    section, values = None, {}
    for line in RCLONE_CONF.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if line.startswith('['):
            section = line.strip('[]')
        elif section == REMOTE_NAME and '=' in line:
            key, value = line.split('=', 1)
            values[key.strip()] = value.strip()
    return values


def token_expiry():
    try:
        expiry = json.loads(read_rclone_conf()['token'])['expiry']
        return dt.datetime.fromisoformat(expiry.replace('Z', '+00:00'))
    except (KeyError, ValueError, TypeError):
        return None


def default_client_id():
    return read_rclone_conf().get('client_id') or defaults().get('client_id', '')


def defaults():
    """Optional pzcloud-defaults.json the world owner ships to friends."""
    try:
        return read_json(DEFAULTS_FILE)
    except (OSError, ValueError):
        return {}


def save_token(client_id, token_text):
    client_id = client_id.strip()
    if not re.fullmatch(r'[0-9a-f]{32}', client_id):
        die('ClientID — это 32 символа 0-9 и a-f со страницы приложения на oauth.yandex.ru.')
    token, expires = parse_token(token_text)
    expiry = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=expires)).strftime('%Y-%m-%dT%H:%M:%SZ')
    # Implicit-flow tokens have no refresh token; rclone needs the field to exist.
    blob = json.dumps({'access_token': token, 'token_type': 'bearer', 'refresh_token': 'none', 'expiry': expiry})
    if not rclone_exe():
        install_rclone()
    staged = RCLONE_CONF.with_name('rclone.conf.new')
    staged.write_text(f'[{REMOTE_NAME}]\ntype = yandex\nclient_id = {client_id}\ntoken = {blob}\n', encoding='utf-8')
    if os.name != 'nt':
        staged.chmod(0o600)
    say('Проверяю доступ к Яндекс Диску…')
    p = subprocess.run([rclone_exe(), 'lsf', REMOTE_NAME + ':', '--max-depth', '1', '--config', str(staged)],
                       capture_output=True, text=True, encoding='utf-8', errors='replace', **NO_WINDOW)
    if p.returncode:
        staged.unlink()
        die('Яндекс Диск не принял токен:\n' + p.stderr.strip()[-500:])
    staged.replace(RCLONE_CONF)
    say(f'Яндекс Диск подключён. Токен действует до {expiry[:10]}.')


def login():
    client_id = input(f'ClientID приложения [{default_client_id()}]: ').strip() or default_client_id()
    url = auth_url(client_id)
    say('Открываю страницу Яндекса. Войдите в СВОЙ аккаунт и нажмите «Разрешить».\n' + url)
    webbrowser.open(url)
    save_token(client_id, input('Вставьте токен или адрес страницы: '))


def cloud_read(cfg, name, required=True):
    p = rc('cat', remote(cfg, name), capture=True, allow_failure=True)
    if p.returncode:
        error = (p.stderr or '').lower()
        # Fail closed on authentication, network, quota or permission errors.
        missing = any(x in error for x in ('object not found', 'directory not found',
                                           'does not exist', 'doesn\'t exist',
                                           'file not found', '404', 'not found'))
        if not required and missing:
            return None
        die('Не удалось прочитать облачный файл: ' + name + '\n' + (p.stderr or '')[-500:])
    try:
        return json.loads(p.stdout)
    except (ValueError, TypeError):
        die('Некорректный JSON в облаке: ' + name)


def cloud_write(cfg, name, data):
    with tempfile.TemporaryDirectory(prefix='pzcloud-json-') as td:
        src = pathlib.Path(td) / 'data.json'
        src.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        rc('copyto', src, remote(cfg, name))
    for _ in range(4):
        check = cloud_read(cfg, name, required=False)
        if check == data:
            return
        time.sleep(2)
    die('Запись в Яндекс Диск не подтвердилась: ' + name + '. Проверьте вручную.')


def cloud_delete(cfg, name):
    rc('deletefile', remote(cfg, name))


def latest(cfg, required=True):
    return cloud_read(cfg, 'latest.json', required=required)


def active(cfg):
    return cloud_read(cfg, 'active.json', required=False)


def check_active(cfg, identity=None):
    a = active(cfg)
    if not a:
        return
    if identity and a.get('id') == identity:
        return
    die('Мир занят пользователем «{}» с {} (идентификатор {}).\n'
        'Не пытайтесь запускать второй хост. Если сессия зависла — согласуйте снятие блокировки.'
        .format(a.get('user', '?'), a.get('time', '?'), a.get('id', '?')))


def files_to_save(cfg):
    root = root_dir(cfg)
    srv = cfg['server']
    world = root / 'Saves' / 'Multiplayer' / srv
    if not world.is_dir():
        die('Папка мира не найдена: ' + str(world) + '. Сначала создайте мир через «Хост».')
    targets = [world]
    server_dir = root / 'Server'
    if server_dir.is_dir():
        targets += [x for x in server_dir.iterdir() if x.is_file() and (x.name == srv + '.ini' or x.name.startswith(srv + '_'))]
    dbdir = root / 'db'
    if dbdir.is_dir():
        targets += [x for x in dbdir.iterdir() if x.is_file() and x.name == srv + '.db']
    return targets


def collect_zip(cfg, output):
    root = root_dir(cfg)
    included = files_to_save(cfg)
    if not any(p.name == cfg['server'] + '.ini' for p in included):
        die('Не найден файл настроек Server/{}.ini. Проверьте имя профиля.'.format(cfg['server']))
    count = 0
    with zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED, compresslevel=3, allowZip64=True) as zf:
        for path in included:
            paths = (p for p in path.rglob('*') if p.is_file() and not p.is_symlink()) if path.is_dir() else [path]
            for file in paths:
                if file.is_symlink():
                    continue
                zf.write(file, file.relative_to(root).as_posix())
                count += 1
    if count == 0:
        die('Архив мира оказался пустым.')
    return count


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as src:
        while chunk := src.read(2**20):
            h.update(chunk)
    return h.hexdigest()


def md5(path):
    h = hashlib.md5()
    with open(path, 'rb') as src:
        while chunk := src.read(2**20):
            h.update(chunk)
    return h.hexdigest()


def verify_cloud_md5(cfg, name, expected):
    # Yandex Disk exposes MD5 via its API. Wait for server-side finalization.
    for _ in range(5):
        p = rc('md5sum', remote(cfg, name), capture=True, allow_failure=True)
        if p.returncode == 0 and p.stdout.strip():
            first = p.stdout.split(None, 1)[0].strip().lower()
            if first == expected:
                return
        time.sleep(3)
    die('Облачный архив не подтвердил совпадение MD5. latest.json не обновлён. Сохранена локальная копия.')


def publish(cfg, zip_path, base, local_id):
    ver = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:10]
    zip_name = f'snapshots/{ver}.zip'
    new_latest = {
        'version': ver, 'archive': zip_name, 'sha256': sha256(zip_path),
        'size': zip_path.stat().st_size, 'parent': base, 'user': cfg['user'], 'time': now(),
        'server': cfg['server'], 'protocol': 1,
    }
    # Check twice to reduce accidental conflicts. Not atomic by itself.
    live = latest(cfg, required=False)
    if (live or {}).get('version') != base:
        die('Последняя версия в облаке изменилась. НЕЛЬЗЯ загружать сохранение поверх неё.')
    rc('copyto', zip_path, remote(cfg, zip_name), '--yandex-upload-wait', '2s')
    verify_cloud_md5(cfg, zip_name, md5(zip_path))
    live = latest(cfg, required=False)
    if (live or {}).get('version') != base:
        die('В процессе загрузки другой пользователь обновил мир. Новый архив остался в snapshots, указатель не менялся.')
    if local_id:
        holder = active(cfg)
        if not holder or holder.get('id') != local_id:
            die('Блокировка хоста изменилась. Публикация отменена, архив сохранён в snapshots.')
    cloud_write(cfg, 'latest.json', new_latest)
    if latest(cfg)['version'] != ver:
        die('Конфликт публикации: latest.json отличается от загруженной версии.')
    return new_latest


def secure_extract(zip_path, dest, server):
    world_prefix = f'Saves/Multiplayer/{server}/'
    config_prefix = f'Server/{server}_'
    exact = {f'Server/{server}.ini', f'db/{server}.db'}
    found_world = False
    found_config = False
    count = 0
    with zipfile.ZipFile(zip_path) as zf:
        infos = zf.infolist()
        if len(infos) > 1000000:
            die('Слишком много файлов в архиве.')
        for entry in infos:
            name = entry.filename.replace('\\', '/')
            pp = pathlib.PurePosixPath(name)
            if (name.startswith('/') or '..' in pp.parts or not
                (name.startswith(world_prefix) or name.startswith(config_prefix) or name in exact)):
                die('Недопустимый путь в архиве: ' + name)
            if entry.is_dir():
                continue
            if name.startswith(world_prefix):
                found_world = True
            if name == f'Server/{server}.ini':
                found_config = True
            if entry.file_size > 8*1024**3:
                die('Подозрительно большой файл в сохранении: ' + name)
            dst = dest.joinpath(*pp.parts)
            dst.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(entry) as src, open(dst, 'wb') as out:
                shutil.copyfileobj(src, out, 2**20)
            count += 1
    if not found_world or not found_config:
        die('В архиве нет полноценного мира или настроек хоста.')
    return count


def install_snapshot(cfg, zip_path):
    root = root_dir(cfg)
    root.mkdir(parents=True, exist_ok=True)
    srv = cfg['server']
    with work_tmp(cfg, 'stage-') as td:
        stage = pathlib.Path(td)
        count = secure_extract(zip_path, stage, srv)
        # Before ANY overwrites, preserve previous local save state.
        LOCAL_BACKUPS.mkdir(parents=True, exist_ok=True)
        backup = LOCAL_BACKUPS / (srv + '-before-import-' + dt.datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:4] + '.zip')
        if (root / 'Saves' / 'Multiplayer' / srv).exists():
            collect_backup(cfg, backup)
            say('Резервная копия локального мира: ' + str(backup))
        incoming_world = stage / 'Saves' / 'Multiplayer' / srv
        local_world = root / 'Saves' / 'Multiplayer' / srv
        local_world.parent.mkdir(parents=True, exist_ok=True)
        # Swap by rename so an interrupted import never leaves the world half-deleted.
        old_world = stage / 'previous-world'
        if local_world.exists():
            local_world.rename(old_world)
        try:
            shutil.move(str(incoming_world), str(local_world))
        except BaseException:
            if old_world.exists() and not local_world.exists():
                old_world.rename(local_world)
            raise
        local_server = root / 'Server'
        local_server.mkdir(parents=True, exist_ok=True)
        for stale in local_server.iterdir():
            if stale.is_file() and (stale.name == srv + '.ini' or stale.name.startswith(srv + '_')):
                stale.unlink()
        for f in (stage / 'Server').iterdir():
            if f.is_file():
                shutil.copy2(f, local_server / f.name)
        local_db = root / 'db'
        local_db.mkdir(parents=True, exist_ok=True)
        src_db = stage / 'db' / (srv + '.db')
        old_db = local_db / (srv + '.db')
        if src_db.is_file():
            shutil.copy2(src_db, old_db)
        elif old_db.exists():
            old_db.unlink()  # Never silently keep stale account data from the previous host.
        say(f'Установлено {count} файлов. Клиентские папки *_player не тронуты.')


def collect_backup(cfg, target):
    # Backups should still work when the old save is incomplete.
    root = root_dir(cfg)
    world = root / 'Saves' / 'Multiplayer' / cfg['server']
    with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED, compresslevel=2) as zf:
        if world.is_dir():
            for f in world.rglob('*'):
                if f.is_file() and not f.is_symlink():
                    zf.write(f, f.relative_to(root).as_posix())
        srvdir = root / 'Server'
        if srvdir.is_dir():
            for f in srvdir.iterdir():
                if f.is_file() and (f.name == cfg['server'] + '.ini' or f.name.startswith(cfg['server'] + '_')):
                    zf.write(f, f.relative_to(root).as_posix())
        db = root / 'db' / (cfg['server'] + '.db')
        if db.is_file():
            zf.write(db, db.relative_to(root).as_posix())


def guess_zomboid_dir():
    home = pathlib.Path.home()
    native = home / 'Zomboid'
    if os.name == 'nt' or (native / 'Saves').is_dir():
        return native
    # Linux + Proton: the game writes into the Wine prefix instead of ~/Zomboid.
    for steam in (home / '.local/share/Steam', home / '.steam/steam',
                  home / '.var/app/com.valvesoftware.Steam/.local/share/Steam'):
        proton = steam / 'steamapps/compatdata' / GAME_ID / 'pfx/drive_c/users/steamuser/Zomboid'
        if (proton / 'Saves').is_dir():
            return proton
    return native


def setup_defaults():
    """Current settings, else the owner's pzcloud-defaults.json, else guesses."""
    old = read_json(CONFIG_FILE) if CONFIG_FILE.is_file() else {}
    base = {'user': '', 'server': 'servertest', 'remote': REMOTE_NAME + ':PZ-Shared',
            'zomboid_dir': str(guess_zomboid_dir())}
    shipped = {k: v for k, v in defaults().items() if k in ('server', 'remote')}
    return {**base, **shipped, **old}


def save_setup(user, srv, rem, zomboid):
    user, srv, rem = user.strip(), srv.strip(), rem.strip().rstrip('/')
    if not user:
        die('Укажите своё имя — его увидят друзья в статусе.')
    if not SAFE.fullmatch(srv):
        die('Имя сервера допускает только латиницу, цифры, дефис, точку и _.')
    cfg = {'user': user, 'server': srv, 'remote': rem, 'zomboid_dir': str(pathlib.Path(zomboid.strip()).expanduser())}
    if not cfg['remote'].count(':') == 1 or not cfg['remote'].split(':', 1)[0]:
        die('Некорректный путь в облаке (пример: yadisk:PZ-Shared).')
    say('Проверяю доступ к общей папке Яндекс Диска…')
    rc('lsf', remote(cfg))
    atomic_json(CONFIG_FILE, cfg)
    say('Настройки сохранены: ' + str(CONFIG_FILE))


def perform_setup():
    old = setup_defaults()
    def ask(k, label):
        value = input(f'{label} [{old[k]}]: ').strip()
        return value or old[k]
    save_setup(ask('user', 'Твоё имя (латиница или русский)'), ask('server', 'Имя сервера из меню Хост'),
               ask('remote', 'Путь к общей папке'), ask('zomboid_dir', 'Локальная папка Zomboid'))


def bootstrap():
    cfg = config()
    if STATE_FILE.exists():
        die('Сначала завершите старую локальную сессию: «Выгрузить мир» (пункт 4).')
    if latest(cfg, required=False):
        die('В облаке уже есть latest.json. Первичную публикацию делать нельзя!')
    check_active(cfg)
    rc('mkdir', remote(cfg, 'snapshots'))
    with work_tmp(cfg, 'publish-') as td:
        z = pathlib.Path(td) / 'world.zip'
        n = collect_zip(cfg, z)
        say(f'Собрано {n} файлов; архив {z.stat().st_size/2**20:.1f} МиБ.')
        confirm('Опубликовать этот мир как первоначальный?')
        result = publish(cfg, z, None, None)
    say('Мир опубликован, ревизия: ' + result['version'])


def confirm(message):
    if not ASK(message):
        die('Отменено пользователем.')


def status_info():
    cfg = config()
    a = active(cfg)
    l = latest(cfg, required=False)
    expiry = token_expiry()
    return {
        'remote': cfg['remote'], 'user': cfg['user'], 'server': cfg['server'],
        'version': l['version'] if l else None, 'last_host': l.get('user', '?') if l else None,
        'last_time': l.get('time') if l else None,
        'busy': a.get('user', '?') if a else None, 'busy_since': a.get('time') if a else None,
        'mine': bool(a and STATE_FILE.exists() and read_json(STATE_FILE)['claim']['id'] == a.get('id')),
        'session': STATE_FILE.exists(),
        'token_days': (expiry - dt.datetime.now(dt.timezone.utc)).days if expiry else None,
    }


def status():
    s = status_info()
    say('Облако: ' + s['remote'])
    say('Версия: ' + (s['version'] or 'не опубликована'))
    say('Последний хост: ' + (s['last_host'] or '—'))
    say('Хост занят: ' + ((s['busy'] + ' с ' + (s['busy_since'] or '?')) if s['busy'] else 'нет'))
    say('Локальная сессия: ' + ('да' if s['session'] else 'нет'))
    if s['token_days'] is not None:
        say(f'Токен Яндекса истекает через {s["token_days"]} дн.')


# "Host" in PZ starts a separate Java server process (zombie.network.GameServer -coop)
# that keeps saving the world for a while after the game window closes.
GAME_RE = re.compile(r'(?i)projectzomboid(32|64)|projectzomboid\.sh|zomboid\.jar|zombie\.gameStates\.MainScreenState'
                     r'|zombie\.network\.GameServer|ProjectZomboid[\\/]jre(64)?[\\/]bin[\\/]java')


def process_lines():
    if os.name == 'nt':
        # tasklist hides command lines, and the coop server runs as java.exe.
        ps = ['powershell', '-NoProfile', '-NonInteractive', '-Command',
              'Get-CimInstance Win32_Process | ForEach-Object { "$($_.Name) $($_.CommandLine)" }']
        try:
            p = subprocess.run(ps, capture_output=True, text=True, errors='replace', timeout=30, **NO_WINDOW)
            if p.returncode == 0 and p.stdout.strip():
                return p.stdout.splitlines()
        except (OSError, subprocess.TimeoutExpired):
            pass
        p = subprocess.run(['tasklist', '/FO', 'CSV', '/NH'], capture_output=True, text=True, errors='replace', timeout=15, **NO_WINDOW)
        return p.stdout.splitlines()
    p = subprocess.run(['ps', '-eo', 'args='], capture_output=True, text=True, errors='replace', timeout=8)
    return p.stdout.splitlines()


def has_game():
    try:
        lines = process_lines()
    except (OSError, subprocess.TimeoutExpired):
        return False
    return any(GAME_RE.search(ln) for ln in lines
               if not ('pzcloud' in ln.lower() or 'ps -eo' in ln or 'Get-CimInstance' in ln))


def launch_game():
    uri = f'steam://rungameid/{GAME_ID}'
    try:
        if os.name == 'nt':
            os.startfile(uri)
        else:
            opener = shutil.which('xdg-open')
            if opener:
                subprocess.Popen([opener, uri], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            elif shutil.which('steam'):
                subprocess.Popen(['steam', '-applaunch', GAME_ID], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                die('Не найден Steam или xdg-open; откройте игру вручную.')
    except OSError as exc:
        die('Не удалось открыть Steam: ' + str(exc))


def begin():
    cfg = config()
    if STATE_FILE.exists():
        die('Локальная сессия уже есть. Завершите её: «Выгрузить мир» (пункт 4).')
    if has_game():
        die('Project Zomboid уже запущен. Сначала полностью закройте его.')
    l = latest(cfg)
    if l.get('server') != cfg['server']:
        die('Имя сервера отличается от сохранённого в облаке.')
    check_active(cfg)
    claim = {'id': uuid.uuid4().hex, 'user': cfg['user'], 'time': now(), 'base': l['version']}
    cloud_write(cfg, 'active.json', claim)
    time.sleep(3)
    if active(cfg) != claim:
        die('Другой хост забрал право на запуск! Не запускайте мир, согласуйте очередь.')
    try:
        with work_tmp(cfg, 'pull-') as td:
            z = pathlib.Path(td) / 'world.zip'
            rc('copyto', remote(cfg, l['archive']), z)
            if z.stat().st_size != l['size'] or sha256(z) != l['sha256']:
                die('Размер/контрольная сумма архива не совпали. Мир не изменён.')
            say('Загружена версия ' + l['version'] + ' (' + str(round(z.stat().st_size/2**20, 1)) + ' МиБ).')
            install_snapshot(cfg, z)
        atomic_json(STATE_FILE, {'claim': claim, 'base': l['version']})
    except Exception:
        # Clear lock only if it is ours. If clearing fails, user can recover manually.
        if active(cfg) == claim:
            cloud_delete(cfg, 'active.json')
        raise
    say('Мир установлен. Открываю Steam / Project Zomboid…')
    launch_game()
    say('Откройте Хост и выберите профиль ' + cfg['server'] + '. Остальные входят через Steam.')
    say('ВАЖНО: после игры дождитесь полного выхода из Project Zomboid.')
    say('Автоматически отслеживаю процесс игры (не на всех конфигурациях Steam/Proton надёжно).')
    for _ in range(90):
        if has_game():
            break
        time.sleep(2)
    else:
        say('Процесс игры не обнаружен. Не публикую файлы автоматически: после игры нажмите «Выгрузить мир» (пункт 4).')
        return
    say('Игра запущена; ожидаю её закрытия…')
    while has_game():
        time.sleep(5)
    time.sleep(6)  # allow the hosting subsystem to flush its files
    say('Игра закрыта. Публикую мир…')
    finish()


def finish():
    cfg = config()
    if not STATE_FILE.exists():
        die('Нет начатой сессии. Сначала «Играть — стать хостом» (пункт 3).')
    state = read_json(STATE_FILE)
    if has_game():
        die('Project Zomboid ещё запущен. Сначала полностью выйдите из игры.')
    claim = state['claim']
    holder = active(cfg)
    if not holder or holder.get('id') != claim['id']:
        die('Блокировка принадлежит другому игроку или была снята. НЕЛЬЗЯ публиковать эту копию.')
    if latest(cfg)['version'] != state['base']:
        die('В облаке появилась другая версия. НЕЛЬЗЯ публиковать поверх неё.')
    LOCAL_BACKUPS.mkdir(parents=True, exist_ok=True)
    z = LOCAL_BACKUPS / (cfg['server'] + '-outgoing-' + dt.datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:6] + '.zip')
    n = collect_zip(cfg, z)
    say(f'Сохранено {n} файлов; локальная копия {z} ({z.stat().st_size/2**20:.1f} МиБ).')
    result = publish(cfg, z, state['base'], claim['id'])
    say('Обновлён latest.json: ' + result['version'])
    if active(cfg) and active(cfg).get('id') == claim['id']:
        cloud_delete(cfg, 'active.json')
    STATE_FILE.unlink()
    say('ГОТОВО: теперь другой друг может брать последнюю версию и играть.')


def cancel():
    cfg = config()
    if not STATE_FILE.exists():
        die('Нет локальной сессии.')
    if has_game():
        die('Игра ещё работает!')
    confirm('Отменить свою сессию БЕЗ выгрузки сохранения? Изменения пропадут из облака.')
    claim = read_json(STATE_FILE)['claim']
    holder = active(cfg)
    if holder and holder.get('id') == claim['id']:
        cloud_delete(cfg, 'active.json')
    STATE_FILE.unlink()
    say('Отмена выполнена. Локальные данные не удалены.')


def force_unlock():
    cfg = config()
    holder = active(cfg)
    if not holder:
        say('В облаке нет блокировки.')
        return
    say('ВНИМАНИЕ: блокировка хоста «{}» с {}'.format(holder.get('user'), holder.get('time')))
    say('Сначала убедитесь в общем чате, что игра закрыта у всех и никто не публикует мир.')
    confirm('Снять блокировку принудительно?')
    if active(cfg) != holder:
        die('Блокировка изменилась, отмена.')
    cloud_delete(cfg, 'active.json')
    say('Блокировка снята. Версия мира не менялась.')


ERRORS = (RuntimeError, OSError, ValueError, EOFError, KeyError, StopIteration, zipfile.BadZipFile)


def menu():
    say('PZ CLOUD COOP ' + VERSION + ' | Linux / Windows | Яндекс Диск')
    while True:
        print('\n8 — Подключить Яндекс Диск (получить токен)\n1 — Первоначальная настройка\n2 — Первая публикация существующего мира (только исходный хост)'
              '\n3 — Скачать мир, открыть Steam и выгрузить при выходе'
              '\n4 — Закончить сессию и выгрузить вручную\n5 — Статус мира и хоста'
              '\n6 — Отменить свою сессию без выгрузки\n7 — Принудительно снять зависшую блокировку\n0 — Выход')
        choice = input('Выбор: ').strip()
        if choice == '0':
            return
        funcs = {'1': perform_setup, '2': bootstrap, '3': begin, '4': finish, '5': status,
                 '6': cancel, '7': force_unlock, '8': login}
        if choice not in funcs:
            continue
        try:
            funcs[choice]()
        except ERRORS as exc:
            say('\nОШИБКА: ' + str(exc))


def main():
    cli = argparse.ArgumentParser(description='PZ Cloud Coop — сохранения Project Zomboid через Яндекс Диск')
    cli.add_argument('command', nargs='?', default='menu',
                     choices=['menu', 'login', 'setup', 'bootstrap', 'start', 'finish', 'status', 'cancel', 'unlock'])
    args = cli.parse_args()
    actions = {'menu': menu, 'login': login, 'setup': perform_setup, 'bootstrap': bootstrap, 'start': begin,
               'finish': finish, 'status': status, 'cancel': cancel, 'unlock': force_unlock}
    try:
        actions[args.command]()
    except ERRORS as exc:
        say('ОШИБКА: ' + str(exc))
        sys.exit(1)


if __name__ == '__main__':
    main()
