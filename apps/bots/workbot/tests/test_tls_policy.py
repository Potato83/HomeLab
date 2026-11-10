"""Real self-signed TLS fixture in an isolated loopback namespace."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import socket
import ssl
import subprocess
from threading import Thread
from unittest.mock import patch
import pytest
from workbot.http import FetchError, HttpClient, russian_host

@pytest.mark.parametrize('url,expected', [
    ('https://example.ru/path',True), ('https://EXAMPLE.RU./',True),
    ('https://пример.рф/',True), ('https://xn--e1afmkfd.xn--p1ai/',True),
    ('https://example.ru.evil.test/',False), ('https://notru.example/',False),
    ('https://127.0.0.1/',False), ('https://api.telegram.org/',False),
])
def test_russian_domain_scope(url,expected):
    assert russian_host(url) is expected

def test_real_self_signed_tls_and_redirect_policy(tmp_path):
    cert,key = tmp_path/'cert.pem',tmp_path/'key.pem'
    subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1',
                    '-subj','/CN=fixture.ru','-config','/dev/null',
                    '-keyout',str(key),'-out',str(cert)],check=True,
                   stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=10)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_GET(self):
            if self.path=='/redirect':
                self.send_response(302)
                self.send_header('Location',f'https://fixture.example:{self.server.server_port}/')
                self.end_headers()
            else:
                self.send_response(200);self.end_headers();self.wfile.write(b'OK')
    server=HTTPServer(('127.0.0.1',0),Handler)
    context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert,key)
    server.socket=context.wrap_socket(server.socket,server_side=True)
    thread=Thread(target=server.serve_forever,daemon=True);thread.start()
    original=socket.getaddrinfo
    def resolve(host,*args,**kwargs):
        return original('127.0.0.1' if host in ('fixture.ru','fixture.example') else host,*args,**kwargs)
    try:
        with patch('socket.getaddrinfo',side_effect=resolve), patch('urllib.request.getproxies',return_value={}):
            ru=f'https://fixture.ru:{server.server_port}/'
            foreign=f'https://fixture.example:{server.server_port}/'
            relaxed=HttpClient(delay=0,timeout=3,verify_ru_tls=False)
            assert relaxed._get_once(ru)=='OK'
            with pytest.raises(FetchError,match='TLS'):
                relaxed._get_once(foreign)
            with pytest.raises(FetchError,match='TLS'):
                HttpClient(delay=0,timeout=3,verify_ru_tls=True)._get_once(ru)
            with pytest.raises(FetchError,match='TLS'):
                relaxed._get_once(ru+'redirect',follow_redirects=True)
    finally:
        server.shutdown();server.server_close();thread.join(timeout=2)
