"""User-scoped Windows DPAPI credentials, outside the project and EEG snapshots."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import uuid


def credential_path(kind='s3'):
    if kind not in ('s3','smb'):raise ValueError('Unknown credential kind')
    if os.name != 'nt' or not os.environ.get('LOCALAPPDATA'):
        raise RuntimeError('Windows LOCALAPPDATA is required for protected upload credentials')
    return Path(os.environ['LOCALAPPDATA']) / 'CFIMAGE' / (kind+'-upload.dpapi')


def protect_bytes(data: bytes, *, decrypt=False):
    if os.name != 'nt':
        raise RuntimeError('DPAPI requires Windows')
    class Blob(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_ubyte))]
    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    result = Blob()
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    try:
        # CRYPTPROTECT_UI_FORBIDDEN; never machine scope, never prompt an operator.
        if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
            raise RuntimeError('Cannot read/write protected upload credentials for this Windows user')
        return ctypes.string_at(result.data, result.size)
    finally:
        ctypes.memset(buffer, 0, len(buffer))
        if result.data:
            ctypes.memset(result.data, 0, result.size)
            kernel.LocalFree(result.data)


def validate(value, config):
    if value.get('schema_version') != 1:
        raise ValueError('Unsupported protected credential format')
    # Do not send the stored account to an endpoint changed in a local config.
    for field in ('endpoint', 'data_bucket', 'prefix'):
        if value.get(field) != config[field]:
            raise ValueError('Upload credential destination does not match configuration')
    for field in ('access_key', 'secret_key'):
        if not isinstance(value.get(field), str) or not value[field].strip():
            raise ValueError('Incomplete upload credential')
    return dict(aws_access_key_id=value['access_key'], aws_secret_access_key=value['secret_key'])


def save_credentials(value, config, path=None):
    validate(value, config)
    path = Path(path) if path is not None else credential_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    encrypted = protect_bytes(json.dumps(value).encode('utf-8'))
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        temporary.write_bytes(encrypted)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def load_credentials(config, path=None):
    path = Path(path) if path is not None else credential_path()
    if not path.exists():
        return None
    try:
        value = json.loads(protect_bytes(path.read_bytes(), decrypt=True))
    except Exception:
        raise RuntimeError('上传授权无法解密。请用当前 Windows 账号重新运行私有授权包。') from None
    return validate(value, config)


def authenticated_session(config):
    import boto3
    # An explicit profile remains useful for administrators and other platforms.
    if config.get('profile'):
        return boto3.Session(profile_name=config['profile'])
    protected = load_credentials(config) if os.name == 'nt' else None
    if protected:
        # A fresh botocore session ignores an unrelated AWS_PROFILE on the PC.
        import botocore.session
        from botocore.configprovider import ConstantProvider
        core = botocore.session.get_session()
        core.get_component('config_store').set_config_provider('profile', ConstantProvider(None))
        core.set_config_variable('config_file', os.devnull)
        core.set_config_variable('credentials_file', os.devnull)
        return boto3.Session(botocore_session=core, **protected)
    if os.environ.get('AWS_ACCESS_KEY_ID') and os.environ.get('AWS_SECRET_ACCESS_KEY'):
        return boto3.Session(aws_access_key_id=os.environ['AWS_ACCESS_KEY_ID'],
                             aws_secret_access_key=os.environ['AWS_SECRET_ACCESS_KEY'],
                             aws_session_token=os.environ.get('AWS_SESSION_TOKEN'))
    raise RuntimeError('尚未安装 S3 上传授权。请用采集时的 Windows 账号运行私有一键授权包；无需登录 SMB。')


SMB_SHARE = r'\\172.16.4.2\aim'


def install_bundled_authorization(root):
    """Also supports starting the upload module without the bootstrap BAT."""
    import subprocess
    package=Path(root)/'.dispatch/upload-authorization.json'
    if package.is_file():
        result=subprocess.run(['powershell.exe','-NoProfile','-ExecutionPolicy','Bypass',
            '-File',str(Path(root)/'scripts/install_upload_authorization.ps1'),'-Package',str(package)],
            capture_output=True,creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:raise RuntimeError('自动安装上传授权失败，请重新安装授权包。')


def smb_connection(username,password):
    class Resource(ctypes.Structure):
        _fields_=[('scope',wintypes.DWORD),('type',wintypes.DWORD),('display',wintypes.DWORD),
            ('usage',wintypes.DWORD),('local',wintypes.LPWSTR),('remote',wintypes.LPWSTR),
            ('comment',wintypes.LPWSTR),('provider',wintypes.LPWSTR)]
    resource=Resource();resource.type=1;resource.remote=SMB_SHARE
    function=ctypes.WinDLL('mpr').WNetAddConnection2W
    function.argtypes=[ctypes.POINTER(Resource),wintypes.LPCWSTR,wintypes.LPCWSTR,wintypes.DWORD]
    function.restype=wintypes.DWORD
    code=function(ctypes.byref(resource),password,username,0)
    if code==1219:
        raise RuntimeError('NAS 已被其他账号连接（Windows 1219）。请关闭该账号的 NAS 连接后重试；程序不会中断其他文件传输。')
    if code:raise RuntimeError(f'NAS 自动登录失败（Windows {code}）。请检查公司网络与上传授权。')


def ensure_smb_access(destination, root):
    value=str(destination)
    if not value.startswith('\\\\'):return  # Local store used by tests/tools.
    if value.startswith('\\\\?\\UNC\\'):value='\\\\'+value[8:]
    if value.casefold()!=SMB_SHARE.casefold() and not value.casefold().startswith(SMB_SHARE.casefold()+'\\'):
        raise ValueError('Refusing to send NAS credentials to a different server/share')
    install_bundled_authorization(root)
    path=credential_path('smb')
    if path.is_file():
        try:
            auth=json.loads(protect_bytes(path.read_bytes(),decrypt=True))
            if auth.get('schema_version')!=1 or auth.get('kind')!='smb' or auth.get('share')!=SMB_SHARE:
                raise ValueError('Invalid SMB credential')
            if not auth.get('username') or not auth.get('password'):raise ValueError('Incomplete SMB credential')
        except Exception:
            raise RuntimeError('本机 NAS 授权无法读取，请重新安装上传授权包。') from None
        smb_connection(auth['username'],auth['password'])
    else:
        # Existing Windows logon remains supported, without disconnecting anything.
        try:Path(SMB_SHARE).stat()
        except OSError:
            raise RuntimeError('尚未安装 NAS 上传授权，且当前 Windows 未连接 NAS。请运行带授权的分发版本。') from None
