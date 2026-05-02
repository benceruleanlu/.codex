#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import random
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional

DEFAULT_BACKEND_REPO = 'https://github.com/comfyanonymous/ComfyUI.git'
DEFAULT_BACKEND_REF = 'master'
DEFAULT_HOST = '127.0.0.1'
BACKEND_READY_PATH = '/api/users'
PREFERRED_PYTHON = '3.12'
DEFAULT_FRONTEND_REPO = Path.cwd().resolve()
DEFAULT_CLOUD_DEV_SERVER_URL = 'https://testcloud.comfy.org/'
DEFAULT_MANAGER_PACKAGE = 'comfyui-manager==4.2.1'


def resolve_codex_home() -> Path:
  return Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex').resolve()


def state_root() -> Path:
  return resolve_codex_home() / 'comfyui-preview-env'


def repo_cache_dir() -> Path:
  return state_root() / 'cache' / 'repos'


def worktrees_dir() -> Path:
  return state_root() / 'runtime' / 'worktrees'


def venvs_dir() -> Path:
  return state_root() / 'runtime' / 'venvs'


def instances_dir() -> Path:
  return state_root() / 'runtime' / 'instances'


def ensure_state_dirs() -> None:
  for path in (
    repo_cache_dir(),
    worktrees_dir(),
    venvs_dir(),
    instances_dir(),
  ):
    path.mkdir(parents=True, exist_ok=True)


def sanitize_name(raw: str) -> str:
  normalized = re.sub(r'[^a-zA-Z0-9_.-]+', '-', raw.strip()).strip('-')
  if not normalized:
    raise SystemExit('instance name cannot be empty')
  return normalized


def generate_name(prefix: str) -> str:
  return sanitize_name(
    f'{prefix}-{int(time.time())}-{random.randint(1000, 9999)}'
  )


def instance_dir(name: str) -> Path:
  return instances_dir() / sanitize_name(name)


def run_id() -> str:
  return f"{int(time.time())}-{random.randint(1000, 9999)}"


def run_dir(name: str, run: str) -> Path:
  return instance_dir(name) / 'runs' / run


def instance_file(name: str) -> Path:
  return instance_dir(name) / 'instance.json'


def read_instance(name: str) -> Optional[Dict[str, Any]]:
  path = instance_file(name)
  if not path.exists():
    return None
  return json.loads(path.read_text())


def write_instance(name: str, data: Dict[str, Any]) -> None:
  path = instance_file(name)
  path.parent.mkdir(parents=True, exist_ok=True)
  path.write_text(json.dumps(data, indent=2, sort_keys=True) + '\n')


def command_exists(name: str) -> bool:
  return shutil.which(name) is not None


def run(
  cmd: list[str],
  *,
  cwd: Optional[Path] = None,
  env: Optional[Dict[str, str]] = None,
  capture: bool = False,
) -> str:
  result = subprocess.run(
    cmd,
    cwd=str(cwd) if cwd else None,
    env=env,
    check=True,
    text=True,
    capture_output=capture,
  )
  return result.stdout if capture else ''


def parse_key_value_pairs(values: Optional[list[str]], flag_name: str) -> Dict[str, str]:
  parsed: Dict[str, str] = {}
  for value in values or []:
    if '=' not in value:
      raise SystemExit(f'{flag_name} values must be KEY=VALUE, got: {value}')
    key, raw = value.split('=', 1)
    key = key.strip()
    if not key:
      raise SystemExit(f'{flag_name} values must include a non-empty KEY')
    parsed[key] = raw
  return parsed


def choose_port(requested: Optional[int]) -> int:
  if requested:
    return requested
  with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
    sock.bind((DEFAULT_HOST, 0))
    sock.listen(1)
    return int(sock.getsockname()[1])


def process_alive(pid: Optional[int]) -> bool:
  if not pid:
    return False
  try:
    os.kill(pid, 0)
  except OSError:
    return False
  return True


def wait_for_http(url: str, timeout: int) -> None:
  deadline = time.time() + timeout
  while time.time() < deadline:
    try:
      with urllib.request.urlopen(url, timeout=2) as response:
        if 200 <= response.status < 500:
          return
    except (urllib.error.URLError, TimeoutError):
      pass
    time.sleep(1)
  raise SystemExit(f'timed out waiting for {url}')


def git_dir_name(repo_url: str) -> str:
  slug = repo_url.rstrip('/').rsplit('/', 1)[-1]
  slug = re.sub(r'\.git$', '', slug)
  return f'{slug}.git'


def ensure_repo_cache(repo_url: str) -> Path:
  repo_dir = repo_cache_dir() / git_dir_name(repo_url)
  if not repo_dir.exists():
    run(['git', 'init', '--bare', str(repo_dir)])
    run(['git', '--git-dir', str(repo_dir), 'remote', 'add', 'origin', repo_url])
  else:
    run(['git', '--git-dir', str(repo_dir), 'remote', 'set-url', 'origin', repo_url])
  return repo_dir


def fetch_commit(repo_dir: Path, ref: str) -> str:
  run(
    [
      'git',
      '--git-dir',
      str(repo_dir),
      'fetch',
      '--prune',
      'origin',
      ref,
    ]
  )
  return run(
    ['git', '--git-dir', str(repo_dir), 'rev-parse', 'FETCH_HEAD'],
    capture=True,
  ).strip()


def cleanup_worktree(repo_dir: Path, worktree_dir: Path) -> None:
  if worktree_dir.exists():
    try:
      run(
        ['git', '--git-dir', str(repo_dir), 'worktree', 'remove', '--force', str(worktree_dir)]
      )
    except subprocess.CalledProcessError:
      shutil.rmtree(worktree_dir, ignore_errors=True)
  run(['git', '--git-dir', str(repo_dir), 'worktree', 'prune'])


def ensure_worktree(repo_dir: Path, name: str, commit: str) -> Path:
  worktree_dir = worktrees_dir() / sanitize_name(name)
  cleanup_worktree(repo_dir, worktree_dir)
  run(
    [
      'git',
      '--git-dir',
      str(repo_dir),
      'worktree',
      'add',
      '--force',
      '--detach',
      str(worktree_dir),
      commit,
    ]
  )
  return worktree_dir


def detect_frontend_repo(explicit: Optional[str]) -> Optional[Path]:
  candidates = [Path(explicit).expanduser()] if explicit else [DEFAULT_FRONTEND_REPO]
  for candidate in candidates:
    if (candidate / 'package.json').exists() and (candidate / 'vite.config.mts').exists():
      return candidate.resolve()
  return None


def ensure_devtools_link(worktree_dir: Path, frontend_repo: Optional[Path]) -> None:
  if frontend_repo is None:
    return
  source = frontend_repo / 'tools' / 'devtools'
  if not source.is_dir():
    return
  target = worktree_dir / 'custom_nodes' / 'ComfyUI_devtools'
  target.parent.mkdir(parents=True, exist_ok=True)
  if target.is_symlink() and target.resolve() == source.resolve():
    return
  if target.exists() or target.is_symlink():
    if target.is_dir() and not target.is_symlink():
      shutil.rmtree(target)
    else:
      target.unlink()
  target.symlink_to(source, target_is_directory=True)


def backend_python_path(venv_dir: Path) -> Path:
  return venv_dir / 'bin' / 'python'


def version_tuple(version: str) -> tuple[int, int]:
  major, minor = version.split('.', 1)
  return int(major), int(minor)


def required_python_spec(worktree_dir: Path) -> str:
  pyproject = worktree_dir / 'pyproject.toml'
  if not pyproject.exists():
    return f'{sys.version_info.major}.{sys.version_info.minor}'

  match = re.search(
    r'^requires-python\s*=\s*"[^"]*>=\s*([0-9]+\.[0-9]+)',
    pyproject.read_text(),
    re.MULTILINE,
  )
  if not match:
    return f'{sys.version_info.major}.{sys.version_info.minor}'

  minimum = match.group(1)
  if version_tuple(PREFERRED_PYTHON) >= version_tuple(minimum):
    return PREFERRED_PYTHON
  return minimum


def ensure_backend_venv(worktree_dir: Path, commit: str) -> tuple[Path, str]:
  python_spec = required_python_spec(worktree_dir)
  venv_dir = venvs_dir() / f"py{python_spec.replace('.', '')}-{commit[:12]}"
  python_bin = backend_python_path(venv_dir)
  stamp = venv_dir / '.ready'
  if stamp.exists() and python_bin.exists():
    return venv_dir, python_spec

  if command_exists('uv'):
    run(['uv', 'python', 'install', python_spec])
    run(['uv', 'venv', str(venv_dir), '--python', python_spec])
    run(
      [
        'uv',
        'pip',
        'install',
        '--python',
        str(python_bin),
        '-r',
        str(worktree_dir / 'requirements.txt'),
      ]
    )
  else:
    host_python = f'{sys.version_info.major}.{sys.version_info.minor}'
    if version_tuple(host_python) < version_tuple(python_spec):
      raise SystemExit(
        f'backend requires Python {python_spec}+ but only {host_python} is available; '
        'install uv or a newer python interpreter'
      )
    run([sys.executable, '-m', 'venv', str(venv_dir)])
    run([str(python_bin), '-m', 'pip', 'install', '-r', str(worktree_dir / 'requirements.txt')])

  stamp.write_text(commit + '\n')
  return venv_dir, python_spec


def install_backend_package(
  *,
  venv_dir: Path,
  package: str,
  stamp_name: str,
) -> None:
  python_bin = backend_python_path(venv_dir)
  stamp = venv_dir / stamp_name
  if stamp.exists() and stamp.read_text().strip() == package:
    return

  if command_exists('uv'):
    run(['uv', 'pip', 'install', '--python', str(python_bin), package])
  else:
    run([str(python_bin), '-m', 'pip', 'install', package])
  stamp.write_text(package + '\n')


def ensure_backend_features(
  *,
  venv_dir: Path,
  enable_manager: bool,
  manager_package: str,
) -> Dict[str, Any]:
  features: Dict[str, Any] = {
    'manager': {
      'enabled': enable_manager,
      'package': manager_package if enable_manager else None,
    }
  }
  if enable_manager:
    install_backend_package(
      venv_dir=venv_dir,
      package=manager_package,
      stamp_name='.feature-manager',
    )
  return features


def start_process(
  cmd: list[str],
  *,
  cwd: Path,
  env: Dict[str, str],
  log_path: Path,
) -> int:
  log_path.parent.mkdir(parents=True, exist_ok=True)
  with log_path.open('ab') as log_file:
    proc = subprocess.Popen(
      cmd,
      cwd=str(cwd),
      env=env,
      stdout=log_file,
      stderr=subprocess.STDOUT,
      start_new_session=True,
    )
  return int(proc.pid)


def stop_pid(pid: Optional[int]) -> None:
  if not process_alive(pid):
    return
  assert pid is not None
  try:
    os.killpg(pid, signal.SIGTERM)
  except ProcessLookupError:
    return

  deadline = time.time() + 10
  while time.time() < deadline:
    if not process_alive(pid):
      return
    time.sleep(0.5)

  try:
    os.killpg(pid, signal.SIGKILL)
  except ProcessLookupError:
    return


def ensure_frontend_dependencies(frontend_repo: Path, install_mode: str) -> None:
  node_modules = frontend_repo / 'node_modules'
  if install_mode == 'never':
    return
  if install_mode == 'missing' and node_modules.exists():
    return
  env = os.environ.copy()
  env.setdefault('CI', '1')
  run(['pnpm', 'install', '--frozen-lockfile'], cwd=frontend_repo, env=env)


def instance_summary(record: Dict[str, Any]) -> Dict[str, Any]:
  backend = record.get('backend', {})
  frontend = record.get('frontend') or {}
  return {
    **record,
    'backend': {
      **backend,
      'alive': process_alive(backend.get('pid')),
      'ready': backend.get('ready', process_alive(backend.get('pid'))),
    },
    'frontend': (
      {
        **frontend,
        'alive': process_alive(frontend.get('pid')),
        'ready': frontend.get('ready', process_alive(frontend.get('pid'))),
      }
      if frontend
      else None
    ),
  }


def print_result(data: Dict[str, Any], json_mode: bool) -> None:
  if json_mode:
    print(json.dumps(data, indent=2, sort_keys=True))
    return

  print(f"name: {data['name']}")
  print(f"mode: {data['mode']}")
  if data.get('frontend_mode'):
    print(f"frontend-mode: {data['frontend_mode']}")
  print(f"run: {data['run_id']}")
  print(f"backend: ready {data['backend']['url']}")
  if data.get('frontend'):
    print(f"frontend: ready {data['frontend']['url']}")
  else:
    print('frontend: -')
  print(f"worktree: {data['paths']['worktree_dir']}")
  print(f"logs: {data['paths']['logs_dir']}")


def build_env(
  *,
  backend_url: str,
  worktree_dir: Path,
  dev_server_url: Optional[str] = None,
  frontend_url: Optional[str] = None,
) -> Dict[str, str]:
  dev_server_url = dev_server_url or backend_url
  env = {
    'COMFYUI_BACKEND_URL': backend_url,
    'DEV_SERVER_COMFYUI_URL': dev_server_url,
    'TEST_COMFYUI_DIR': str(worktree_dir),
  }
  env['PLAYWRIGHT_TEST_URL'] = frontend_url or backend_url
  if frontend_url:
    env['COMFYUI_FRONTEND_URL'] = frontend_url
  return env


def feature_matches(
  record: Dict[str, Any],
  *,
  enable_manager: bool,
  manager_package: str,
) -> bool:
  features = record.get('features') or {}
  manager = features.get('manager') or {}
  if bool(manager.get('enabled')) != enable_manager:
    return False
  if enable_manager and manager.get('package') != manager_package:
    return False
  return True


def base_record(
  *,
  name: str,
  mode: str,
  frontend_mode: Optional[str],
  run: str,
  repo_url: str,
  ref: str,
  commit: str,
  worktree_dir: Path,
  venv_dir: Path,
  python_spec: str,
  backend_port: int,
  backend_pid: int,
  backend_log: Path,
  backend_url: str,
  backend_args: list[str],
  features: Dict[str, Any],
) -> Dict[str, Any]:
  run_path = run_dir(name, run)
  record = {
    'name': name,
    'mode': mode,
    'frontend_mode': frontend_mode,
    'run_id': run,
    'repo_url': repo_url,
    'ref': ref,
    'commit': commit,
    'updated_at': int(time.time()),
    'backend': {
      'pid': backend_pid,
      'port': backend_port,
      'url': backend_url,
      'log_path': str(backend_log),
      'python_spec': python_spec,
      'args': backend_args,
      'ready': True,
    },
    'features': features,
    'frontend': None,
    'paths': {
      'instance_dir': str(instance_dir(name)),
      'instance_file': str(instance_file(name)),
      'worktree_dir': str(worktree_dir),
      'venv_dir': str(venv_dir),
      'run_dir': str(run_path),
      'logs_dir': str(run_path / 'logs'),
    },
  }
  record['env'] = build_env(
    backend_url=backend_url,
    worktree_dir=worktree_dir,
  )
  return record


def ensure_backend_instance(
  *,
  name: str,
  repo_url: str,
  ref: str,
  backend_port: Optional[int],
  frontend_repo: Optional[Path],
  enable_manager: bool,
  manager_package: str,
  backend_args: list[str],
  backend_env: Dict[str, str],
  timeout: int,
) -> Dict[str, Any]:
  existing = read_instance(name)
  if existing and process_alive(existing.get('backend', {}).get('pid')):
    if existing.get('repo_url') != repo_url or existing.get('ref') != ref:
      raise SystemExit(
        f'instance {name} is already running with repo/ref '
        f"{existing.get('repo_url')}@{existing.get('ref')}"
      )
    if not feature_matches(
      existing,
      enable_manager=enable_manager,
      manager_package=manager_package,
    ):
      raise SystemExit(
        f'instance {name} is already running with different backend features; '
        f'stop it first or choose a new --name'
      )
    return existing

  run = run_id()
  repo_dir = ensure_repo_cache(repo_url)
  commit = fetch_commit(repo_dir, ref)
  worktree = ensure_worktree(repo_dir, name, commit)
  ensure_devtools_link(worktree, frontend_repo)
  venv, python_spec = ensure_backend_venv(worktree, commit)
  features = ensure_backend_features(
    venv_dir=venv,
    enable_manager=enable_manager,
    manager_package=manager_package,
  )

  chosen_port = choose_port(backend_port)
  backend_url = f'http://{DEFAULT_HOST}:{chosen_port}'
  backend_log = run_dir(name, run) / 'logs' / 'backend.log'
  backend_log.parent.mkdir(parents=True, exist_ok=True)
  env = os.environ.copy()
  env['PYTHONUNBUFFERED'] = '1'
  env.update(backend_env)
  launch_args = ['--multi-user', '--port', str(chosen_port)]
  if enable_manager:
    launch_args.append('--enable-manager')
  launch_args.extend(backend_args)
  pid = start_process(
    [str(backend_python_path(venv)), 'main.py', *launch_args],
    cwd=worktree,
    env=env,
    log_path=backend_log,
  )
  wait_for_http(backend_url + BACKEND_READY_PATH, timeout)

  record = base_record(
    name=name,
    mode='backend',
    frontend_mode=None,
    run=run,
    repo_url=repo_url,
    ref=ref,
    commit=commit,
    worktree_dir=worktree,
    venv_dir=venv,
    python_spec=python_spec,
    backend_port=chosen_port,
    backend_pid=pid,
    backend_log=backend_log,
    backend_url=backend_url,
    backend_args=launch_args,
    features=features,
  )
  write_instance(name, record)
  return record


def add_frontend_preview(
  record: Dict[str, Any],
  *,
  frontend_repo: Path,
  frontend_mode: str,
  frontend_port: Optional[int],
  frontend_install: str,
  frontend_env: Dict[str, str],
  timeout: int,
) -> Dict[str, Any]:
  existing_frontend = record.get('frontend') or {}
  if process_alive(existing_frontend.get('pid')):
    return record

  ensure_frontend_dependencies(frontend_repo, frontend_install)
  chosen_port = choose_port(frontend_port)
  frontend_url = f'http://{DEFAULT_HOST}:{chosen_port}'
  frontend_log = Path(record['paths']['run_dir']) / 'logs' / 'frontend.log'
  frontend_log.parent.mkdir(parents=True, exist_ok=True)
  dev_server_url = (
    DEFAULT_CLOUD_DEV_SERVER_URL if frontend_mode == 'cloud' else record['backend']['url']
  )
  vite_config = (
    'vite.electron.config.mts' if frontend_mode == 'desktop' else 'vite.config.mts'
  )
  distribution = 'desktop' if frontend_mode == 'desktop' else frontend_mode
  env = os.environ.copy()
  env.update(
    {
      'DISTRIBUTION': distribution,
      'DEV_SERVER_COMFYUI_URL': dev_server_url,
      'BROWSER': 'none',
    }
  )
  env.update(frontend_env)
  pid = start_process(
    [
      'pnpm',
      'exec',
      'vite',
      '--config',
      vite_config,
      '--host',
      DEFAULT_HOST,
      '--port',
      str(chosen_port),
    ],
    cwd=frontend_repo,
    env=env,
    log_path=frontend_log,
  )
  wait_for_http(frontend_url, timeout)

  record['mode'] = 'preview'
  record['frontend'] = {
    'pid': pid,
    'port': chosen_port,
    'url': frontend_url,
    'log_path': str(frontend_log),
    'repo_path': str(frontend_repo),
    'mode': frontend_mode,
    'install': frontend_install,
    'vite_config': vite_config,
    'ready': True,
  }
  record['env'] = build_env(
    backend_url=record['backend']['url'],
    dev_server_url=dev_server_url,
    frontend_url=frontend_url,
    worktree_dir=Path(record['paths']['worktree_dir']),
  )
  record['updated_at'] = int(time.time())
  write_instance(record['name'], record)
  return record


def cmd_start(args: argparse.Namespace) -> None:
  ensure_state_dirs()
  name = sanitize_name(args.name or generate_name('backend'))
  frontend_repo = detect_frontend_repo(args.frontend_repo)
  record = ensure_backend_instance(
    name=name,
    repo_url=args.backend_repo,
    ref=args.backend_ref,
    backend_port=args.backend_port,
    frontend_repo=frontend_repo,
    enable_manager=args.enable_manager,
    manager_package=args.manager_package,
    backend_args=args.backend_arg or [],
    backend_env=parse_key_value_pairs(args.backend_env, '--backend-env'),
    timeout=args.timeout,
  )
  print_result(instance_summary(record), args.json)


def cmd_preview(args: argparse.Namespace) -> None:
  ensure_state_dirs()
  name = sanitize_name(args.name or generate_name('preview'))
  frontend_repo = detect_frontend_repo(args.frontend_repo)
  if frontend_repo is None:
    raise SystemExit(
      'preview requires --frontend-repo or a current working directory that '
      'contains package.json, vite.config.mts, and tools/devtools'
    )

  frontend_mode = args.frontend_mode

  record = ensure_backend_instance(
    name=name,
    repo_url=args.backend_repo,
    ref=args.backend_ref,
    backend_port=args.backend_port,
    frontend_repo=frontend_repo,
    enable_manager=args.enable_manager,
    manager_package=args.manager_package,
    backend_args=args.backend_arg or [],
    backend_env=parse_key_value_pairs(args.backend_env, '--backend-env'),
    timeout=args.timeout,
  )
  record['frontend_mode'] = frontend_mode
  record = add_frontend_preview(
    record,
    frontend_repo=frontend_repo,
    frontend_mode=frontend_mode,
    frontend_port=args.frontend_port,
    frontend_install=args.frontend_install,
    frontend_env=parse_key_value_pairs(args.frontend_env, '--frontend-env'),
    timeout=args.timeout,
  )
  print_result(instance_summary(record), args.json)


def cmd_status(args: argparse.Namespace) -> None:
  record = read_instance(args.name)
  if record is None:
    raise SystemExit(f'unknown instance: {args.name}')
  print_result(instance_summary(record), args.json)


def cmd_list(args: argparse.Namespace) -> None:
  ensure_state_dirs()
  data = []
  for path in sorted(instances_dir().glob('*/instance.json')):
    record = json.loads(path.read_text())
    data.append(instance_summary(record))

  if args.json:
    print(json.dumps(data, indent=2, sort_keys=True))
    return

  for record in data:
    backend_alive = record['backend']['alive']
    frontend = record.get('frontend')
    frontend_text = frontend['url'] if frontend and frontend['alive'] else '-'
    print(
      f"{record['name']}: mode={record['mode']} "
      f"backend={record['backend']['url']} alive={backend_alive} "
      f"frontend={frontend_text}"
    )


def cmd_stop(args: argparse.Namespace) -> None:
  record = read_instance(args.name)
  if record is None:
    raise SystemExit(f'unknown instance: {args.name}')
  frontend = record.get('frontend') or {}
  stop_pid(frontend.get('pid'))
  stop_pid(record.get('backend', {}).get('pid'))
  updated = instance_summary(record)
  write_instance(args.name, updated)
  print_result(updated, args.json)


def build_parser() -> argparse.ArgumentParser:
  parser = argparse.ArgumentParser(
    description='Manage isolated ComfyUI backend instances and frontend preview servers.'
  )
  subparsers = parser.add_subparsers(dest='command', required=True)

  def add_common_start_flags(subparser: argparse.ArgumentParser) -> None:
    subparser.add_argument('--name', help='Stable instance name for reuse')
    subparser.add_argument(
      '--backend-repo',
      default=DEFAULT_BACKEND_REPO,
      help=f'ComfyUI backend git remote (default: {DEFAULT_BACKEND_REPO})',
    )
    subparser.add_argument(
      '--backend-ref',
      default=DEFAULT_BACKEND_REF,
      help=f'Git ref to checkout (default: {DEFAULT_BACKEND_REF})',
    )
    subparser.add_argument(
      '--backend-port',
      type=int,
      help='Backend port. Defaults to a free port.',
    )
    subparser.add_argument(
      '--frontend-repo',
      default=str(DEFAULT_FRONTEND_REPO),
      help=f'ComfyUI_frontend repo used for preview mode (default: {DEFAULT_FRONTEND_REPO})',
    )
    subparser.add_argument(
      '--timeout',
      type=int,
      default=180,
      help='Startup timeout in seconds (default: 180)',
    )
    subparser.add_argument(
      '--enable-manager',
      action='store_true',
      help='Install and start backend with ComfyUI Manager enabled.',
    )
    subparser.add_argument(
      '--manager-package',
      default=DEFAULT_MANAGER_PACKAGE,
      help=f'Python package spec used with --enable-manager (default: {DEFAULT_MANAGER_PACKAGE})',
    )
    subparser.add_argument(
      '--backend-arg',
      action='append',
      help='Extra argument passed through to backend main.py. Repeat for multiple args.',
    )
    subparser.add_argument(
      '--backend-env',
      action='append',
      help='Extra backend environment variable as KEY=VALUE. Repeat for multiple vars.',
    )
    subparser.add_argument('--json', action='store_true', help='Emit JSON')

  start = subparsers.add_parser('start', help='Start backend only')
  add_common_start_flags(start)
  start.set_defaults(func=cmd_start)

  preview = subparsers.add_parser(
    'preview',
    help='Start backend and a frontend Vite preview server',
  )
  add_common_start_flags(preview)
  preview.add_argument(
    '--frontend-port',
    type=int,
    help='Frontend dev-server port. Defaults to a free port.',
  )
  preview.add_argument(
    '--frontend-mode',
    choices=('localhost', 'cloud', 'desktop'),
    default='localhost',
    help='Frontend runtime mode for preview (default: localhost)',
  )
  preview.add_argument(
    '--frontend-install',
    choices=('missing', 'always', 'never'),
    default='missing',
    help='When to run pnpm install before Vite (default: missing)',
  )
  preview.add_argument(
    '--frontend-env',
    action='append',
    help='Extra frontend environment variable as KEY=VALUE. Repeat for multiple vars.',
  )
  preview.set_defaults(func=cmd_preview)

  status = subparsers.add_parser('status', help='Show one instance')
  status.add_argument('name', help='Instance name')
  status.add_argument('--json', action='store_true', help='Emit JSON')
  status.set_defaults(func=cmd_status)

  list_cmd = subparsers.add_parser('list', help='Show all known instances')
  list_cmd.add_argument('--json', action='store_true', help='Emit JSON')
  list_cmd.set_defaults(func=cmd_list)

  stop = subparsers.add_parser('stop', help='Stop one instance')
  stop.add_argument('name', help='Instance name')
  stop.add_argument('--json', action='store_true', help='Emit JSON')
  stop.set_defaults(func=cmd_stop)

  return parser


def main() -> None:
  parser = build_parser()
  args = parser.parse_args()
  args.func(args)


if __name__ == '__main__':
  main()
