"""Discover an already-running local Web A without replacing its session."""
import hashlib
import json
import os
from pathlib import Path
import secrets
import tempfile
from urllib.parse import urlsplit, parse_qs
from urllib.request import Request, ProxyHandler, HTTPRedirectHandler, build_opener


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def config_key(path):
    return hashlib.sha256(os.path.normcase(str(Path(path).resolve())).encode('utf-8')).hexdigest()


def instance_file(config_path):
    path = Path(config_path).resolve()
    return path.with_name('.'+path.name+'.web-instance.json')


def publish_instance(config_path, server):
    path = instance_file(config_path)
    record = dict(instance=server.instance_id, config=config_key(config_path),
                  url=server.launch_url, pid=os.getpid())
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                     prefix='.web-instance-', delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(record, stream)
    try:
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def existing_url(config_path):
    """Only reopen a loopback URL after authenticated instance verification."""
    try:
        path = instance_file(config_path)
        if path.stat().st_size > 8192:
            return None
        record = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(record, dict) or record.get('config') != config_key(config_path):
            return None
        url = urlsplit(record['url'])
        if (url.scheme != 'http' or url.hostname != '127.0.0.1' or url.username or url.password
                or url.path != '/' or url.query or not url.port or not 1 <= url.port <= 65535):
            return None
        token = parse_qs(url.fragment).get('token', [''])[0]
        if not token:
            return None
        request = Request('http://'+url.netloc+'/api/instance', headers={
            'X-Sawyer-Token': token, 'X-Sawyer-Client': 'launcher-'+secrets.token_hex(12)})
        with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=1) as response:
            live = json.loads(response.read(8192))
        if (isinstance(live, dict) and live.get('service') == 'sawyer-web' and live.get('instance') == record.get('instance')
                and live.get('config') == config_key(config_path)):
            return record['url']
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def remove_instance(config_path, instance_id):
    path = instance_file(config_path)
    try:
        record = json.loads(path.read_text(encoding='utf-8'))
        if record.get('instance') == instance_id:
            path.unlink()
    except (OSError, ValueError):
        pass
