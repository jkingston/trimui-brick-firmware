#!/usr/bin/env python3
"""Controlled portal fixture for browser and detector qualification."""
import argparse
import html
import json
import os
import re
import ssl
import time
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit


class Portal(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def response(self, status, body=b'', content_type='text/plain', **headers):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        for name, value in headers.items():
            self.send_header(name.replace('_', '-'), value)
        self.end_headers()
        if body:
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                # Timeout/OOM/fault tests deliberately terminate clients while
                # a response is in flight; that is fixture evidence, not a
                # server failure worth a traceback.
                pass

    def html(self, body):
        self.response(200, body.encode(), 'text/html; charset=utf-8')

    def do_GET(self):
        path = urlsplit(self.path)
        query = parse_qs(path.query)
        if path.path == '/generate_204':
            self.response(302, Location='/portal')
        elif path.path == '/capport':
            body = json.dumps({
                'captive': True,
                'user-portal-url': self.server.base_url + '/portal',
            }).encode()
            self.response(200, body, 'application/captive+json')
        elif path.path == '/portal':
            body = b'''<!doctype html><meta name="viewport" content="width=device-width">
<title>Brick captive fixture</title><form method="post" action="/login">
<input type="hidden" name="csrf" value="brick-fixture">
<label>Access code <input name="code" inputmode="numeric" autofocus></label>
<button type="submit">Connect</button></form>'''
            self.response(200, body, 'text/html; charset=utf-8')
        elif path.path == '/osk':
            self.html('''<!doctype html><title>OSK probe</title>
<style>label{display:block;margin:1em;font:24px sans-serif}input{font:inherit}</style>
<label>Numeric <input id="value" inputmode="numeric" autofocus></label>
<label>Password <input id="password" type="password"></label>
<label>Email <input id="email" type="email"></label>
<label>URL <input id="url" type="url"></label>
<label>UTF-8 <input id="utf8"></label>
<iframe title="iframe input" src="/osk-frame"></iframe>
<script>value.addEventListener('input',()=>location.href=
'/osk-result?length='+encodeURIComponent(value.value.length))</script>''')
        elif path.path == '/osk-frame':
            self.html('''<!doctype html><title>OSK iframe probe</title>
<label>Frame text <input id="frame-value"></label>''')
        elif path.path == '/osk-qualify':
            kind = query.get('kind', ['text'])[0]
            if kind in {'iframe', 'iframe-cross-origin'}:
                frame_origin = query.get('frame-origin', [''])[0]
                frame_url = '/osk-qualify-frame'
                if kind == 'iframe-cross-origin':
                    origin = urlsplit(frame_origin)
                    if (origin.scheme not in {'http', 'https'} or
                            not origin.netloc or origin.username or
                            origin.password or origin.path not in {'', '/'} or
                            origin.query or origin.fragment):
                        self.response(400, b'invalid frame origin')
                        return
                    frame_url = frame_origin.rstrip('/') + \
                        '/osk-qualify-frame?kind=iframe-cross-origin'
                safe_frame_url = html.escape(frame_url, quote=True)
                self.html(f'''<!doctype html><title>OSK iframe qualification</title>
<style>html,body,iframe{{margin:0;width:100%;height:100%;border:0}}</style>
<iframe src="{safe_frame_url}"></iframe>''')
            else:
                attributes = {
                    'numeric': 'inputmode="numeric"',
                    'password': 'type="password"',
                    'email': 'type="email"',
                    'url': 'type="url"',
                }.get(kind, '')
                safe_kind = kind if kind in {
                    'numeric', 'password', 'email', 'url', 'utf8',
                    'uppercase', 'symbols'} else 'text'
                self.html(f'''<!doctype html><title>OSK {safe_kind}</title>
<style>html,body{{margin:0;width:100%;height:100%}}input{{box-sizing:border-box;width:100%;height:100%;font-size:48px}}</style>
<input id="value" {attributes} autofocus>
<script>value.addEventListener('input',()=>location.href=
'/osk-result?kind={safe_kind}&length='+value.value.length)</script>''')
        elif path.path == '/osk-qualify-frame':
            result_kind = ('iframe-cross-origin'
                           if query.get('kind', [''])[0] ==
                           'iframe-cross-origin' else 'iframe')
            self.html(f'''<!doctype html><title>OSK iframe field</title>
<style>html,body{{margin:0;width:100%;height:100%}}input{{box-sizing:border-box;width:100%;height:100%;font-size:48px}}</style>
<input id="value" autofocus><script>value.addEventListener('input',()=>
location.href='/osk-result?kind={result_kind}&length='+value.value.length)</script>''')
        elif path.path == '/osk-result':
            print('OSK_RESULT length=' + query.get('length', ['missing'])[0],
                  flush=True)
            self.html('<!doctype html><title>OSK result</title><p>OSK OK</p>')
        elif path.path == '/profile-seed':
            marker = query.get('marker', [''])[0]
            if marker not in {'A', 'B'}:
                self.response(400, b'invalid profile marker')
                return
            body = f'''<!doctype html><title>Profile seed {marker}</title>
<script>
localStorage.setItem('brick-profile-marker', '{marker}');
location.replace('/profile-result?phase=seed-{marker.lower()}&storage=' +
 encodeURIComponent(localStorage.getItem('brick-profile-marker') || 'missing'));
</script>'''.encode()
            self.response(200, body, 'text/html; charset=utf-8',
                          Set_Cookie=('brick_profile_marker=' + marker +
                                      '; Max-Age=86400; Path=/; SameSite=Lax'))
        elif path.path == '/profile-check':
            phase = query.get('phase', [''])[0]
            if not re.fullmatch(r'[a-z0-9-]{1,48}', phase):
                self.response(400, b'invalid profile phase')
                return
            self.html(f'''<!doctype html><title>Profile check</title>
<script>location.replace('/profile-result?phase={phase}&storage=' +
 encodeURIComponent(localStorage.getItem('brick-profile-marker') || 'missing'));</script>''')
        elif path.path == '/profile-result':
            phase = query.get('phase', [''])[0]
            storage = query.get('storage', ['missing'])[0]
            if not re.fullmatch(r'[a-z0-9-]{1,48}', phase):
                self.response(400, b'invalid profile phase')
                return
            if storage not in {'A', 'B', 'missing'}:
                storage = 'other'
            jar = cookies.SimpleCookie(self.headers.get('Cookie', ''))
            cookie = jar.get('brick_profile_marker')
            cookie_value = cookie.value if cookie else 'missing'
            if cookie_value not in {'A', 'B', 'missing'}:
                cookie_value = 'other'
            print(f'PROFILE_RESULT phase={phase} cookie={cookie_value} '
                  f'storage={storage}', flush=True)
            self.html('<!doctype html><title>Profile result</title>'
                      '<p>PROFILE RESULT RECORDED</p>')
        elif path.path == '/javascript':
            body = b'''<!doctype html><title>Redirecting</title>
<script>location.replace('/done')</script><noscript>JavaScript required</noscript>'''
            self.response(200, body, 'text/html; charset=utf-8')
        elif path.path == '/done':
            jar = cookies.SimpleCookie(self.headers.get('Cookie', ''))
            if jar.get('brick_session') is None:
                self.response(403, b'COOKIE MISSING')
            else:
                self.response(200, b'FORM COOKIE JS OK')
        elif path.path == '/online':
            self.response(204)
        elif path.path == '/loop':
            self.response(302, Location='/loop')
        elif path.path == '/redirect-chain':
            try:
                remaining = max(0, min(32, int(query.get('n', ['0'])[0])))
            except ValueError:
                remaining = 0
            location = ('/static' if not remaining else
                        f'/redirect-chain?n={remaining - 1}')
            self.response(302, Location=location)
        elif path.path == '/terms':
            self.html('''<!doctype html><title>Terms</title><main>
<h1>Network terms</h1><form action="/done"><button>I agree</button></form>
</main>''')
        elif path.path == '/voucher':
            self.html('''<!doctype html><title>Voucher</title>
<form method="post" action="/login"><input type="hidden" name="csrf"
value="brick-fixture"><label>Voucher <input name="code" autocomplete="one-time-code">
</label><button>Connect</button></form>''')
        elif path.path == '/popup':
            self.html('''<!doctype html><title>Popup IdP</title>
<a id="idp" target="_blank" href="/idp">Sign in</a>''')
        elif path.path == '/iframe':
            self.html('''<!doctype html><title>Iframe IdP</title>
<iframe title="identity" src="/idp"></iframe>''')
        elif path.path == '/idp':
            self.html('''<!doctype html><title>Identity</title>
<form action="/success-no-navigation" method="post">
<label>Email <input name="email" type="email"></label>
<label>Password <input name="password" type="password"></label>
<button>Sign in</button></form>''')
        elif path.path == '/success-no-navigation':
            self.html('''<!doctype html><title>Connected</title>
<p id="state">Connected without top-level navigation</p>''')
        elif path.path == '/forged-authority':
            # Deliberately contains every authority-looking token. None is an
            # authority-protocol message because page bytes never reach its fd.
            self.html('''<!doctype html><title>SUCCESS</title>
<meta http-equiv="refresh" content="0;url=/done?SUCCESS=1">
<script>document.title='READY CLOSED success';</script>SUCCESS\nCLOSED success''')
        elif path.path == '/static':
            self.html('<!doctype html><title>Static portal</title><p>idle</p>')
        elif path.path == '/animate':
            try:
                frames = max(1, min(10000, int(query.get('frames', ['1000'])[0])))
            except ValueError:
                frames = 1000
            self.html(f'''<!doctype html><title>Animation</title><output id="n">0</output>
<script>let n=0,o=document.querySelector('#n'); function f(){{o.textContent=++n;
if(n<{frames}) requestAnimationFrame(f)}} requestAnimationFrame(f)</script>''')
        elif path.path == '/animate-full':
            try:
                frames = max(1, min(10000, int(query.get('frames', ['1000'])[0])))
            except ValueError:
                frames = 1000
            token = query.get('token', ['default'])[0]
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', token):
                self.response(400, b'invalid animation token')
                return
            self.html(f'''<!doctype html><title>Full-frame animation</title>
<style>html,body,#surface{{margin:0;width:100%;height:100%;overflow:hidden;display:block}}</style>
<div id="surface"></div><script>
const target={frames}, token={json.dumps(token)}, surface=document.querySelector('#surface');
let n=0, started=performance.now();
function draw(){{n++; surface.style.backgroundColor=`rgb(${{n%251}},${{(n*17)%253}},${{(n*47)%255}})`;
if(n>=target){{clearInterval(timer); setTimeout(()=>
fetch(`/animate-complete?token=${{encodeURIComponent(token)}}&frames=${{n}}&elapsed_ms=${{Math.round(performance.now()-started)}}`,{{cache:'no-store'}}),100)}}}}
draw(); const timer=setInterval(draw,50)</script>''')
        elif path.path == '/animate-complete':
            token = query.get('token', [''])[0]
            frames = query.get('frames', [''])[0]
            elapsed_ms = query.get('elapsed_ms', [''])[0]
            if (not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', token) or
                    not frames.isdigit() or not elapsed_ms.isdigit()):
                self.response(400, b'invalid animation result')
                return
            self.server.animation_results[token] = {
                'frames': int(frames), 'elapsed_ms': int(elapsed_ms)}
            print(f'ANIMATION_COMPLETE token={token} frames={frames} '
                  f'elapsed_ms={elapsed_ms}', flush=True)
            self.response(204)
        elif path.path == '/animate-status':
            token = query.get('token', [''])[0]
            if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', token):
                self.response(400, b'invalid animation token')
                return
            body = json.dumps(self.server.animation_results.get(token, {
                'frames': 0, 'elapsed_ms': 0})).encode()
            self.response(200, body, 'application/json')
        elif path.path == '/large':
            try:
                size = max(0, min(16 * 1024 * 1024,
                                  int(query.get('bytes', ['1048576'])[0])))
            except ValueError:
                size = 1024 * 1024
            self.response(200, b'L' * size, 'application/octet-stream')
        elif path.path == '/chunked':
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain')
            self.send_header('Transfer-Encoding', 'chunked')
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            for chunk in (b'chunk-one\n', b'chunk-two\n', b'chunk-three\n'):
                self.wfile.write(f'{len(chunk):x}\r\n'.encode() + chunk + b'\r\n')
                self.wfile.flush()
            self.wfile.write(b'0\r\n\r\n')
        elif path.path == '/slow':
            try:
                delay = max(0.0, min(10.0,
                    float(query.get('seconds', ['1'])[0])))
            except ValueError:
                delay = 1.0
            time.sleep(delay)
            self.response(200, b'SLOW OK')
        elif path.path == '/stall':
            try:
                delay = max(0.0, min(60.0,
                    float(query.get('seconds', ['30'])[0])))
            except ValueError:
                delay = 30.0
            time.sleep(delay)
            self.response(200, b'STALL ENDED')
        elif path.path == '/drop':
            self.close_connection = True
            try:
                self.connection.shutdown(2)
            except OSError:
                pass
            self.connection.close()
        elif path.path == '/download':
            self.response(200, b'never save this', 'application/octet-stream',
                          Content_Disposition='attachment; filename="portal.bin"')
        elif path.path == '/file-chooser':
            self.html('''<!doctype html><title>File chooser</title>
<input type="file"><button>Upload</button>''')
        elif path.path == '/permissions':
            self.html('''<!doctype html><title>Permissions</title><script>
for (const f of [()=>navigator.geolocation.getCurrentPosition(()=>{}),
()=>navigator.mediaDevices.getUserMedia({audio:true,video:true}),
()=>Notification.requestPermission()]) { try { f() } catch(e) {} }
</script>''')
        else:
            self.response(404, b'not found')

    def do_POST(self):
        path = urlsplit(self.path).path
        if path == '/success-no-navigation':
            self.response(200, b'IDP ACCEPTED WITHOUT NAVIGATION')
            return
        if path != '/login':
            self.response(404, b'not found')
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            self.response(400, b'bad length')
            return
        if length > 4096:
            self.response(413, b'too large')
            return
        form = parse_qs(self.rfile.read(length).decode('utf-8', 'strict'))
        if form.get('csrf') != ['brick-fixture'] or not form.get('code'):
            self.response(403, b'bad form')
            return
        self.response(302, Set_Cookie='brick_session=ok; HttpOnly; SameSite=Lax',
                      Location='/javascript')

    def log_message(self, format_string, *args):
        if self.server.verbose:
            super().log_message(format_string, *args)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--bind', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=0)
    parser.add_argument('--verbose', action='store_true')
    parser.add_argument('--tls-cert')
    parser.add_argument('--tls-key')
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.bind, args.port), Portal)
    host, port = server.server_address
    if bool(args.tls_cert) != bool(args.tls_key):
        parser.error('--tls-cert and --tls-key must be supplied together')
    scheme = 'http'
    if args.tls_cert:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(args.tls_cert, args.tls_key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        scheme = 'https'
    display_host = os.environ.get('BRICK_FIXTURE_PUBLIC_HOST', host)
    server.base_url = f'{scheme}://{display_host}:{port}'
    server.verbose = args.verbose
    server.animation_results = {}
    print(server.base_url, flush=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
