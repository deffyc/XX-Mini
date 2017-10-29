import errno
import socket
import ssl
import urlparse
import base64

import OpenSSL
NetWorkIOError = (socket.error, ssl.SSLError, OpenSSL.SSL.Error, OSError)

from xlog import getLogger
xlog = getLogger("gae_proxy")
import simple_http_client
import simple_http_server
from cert_util import CertUtil
from config import config
import gae_handler
import direct_handler
from connect_control import touch_active


class BaseProxyHandlerFilter(object):
    """base proxy handler filter"""
    def filter(self, handler):
        raise NotImplementedError


class AuthFilter(BaseProxyHandlerFilter):
    """authorization filter"""
    auth_info = "Proxy authentication required"
    white_list = ['127.0.0.1']

    def __init__(self, username, password):
        self.username = username
        self.password = password

    def check_auth_header(self, auth_header):
        method, _, auth_data = auth_header.partition(' ')
        if method == 'Basic':
            username, _, password = base64.b64decode(auth_data).partition(':')
            if username == self.username and password == self.password:
                return True
        return False

    def filter(self, handler):
        if handler.client_address[0] in self.white_list:
            return None
        auth_header = handler.headers.get('Proxy-Authorization') or getattr(handler, 'auth_header', None)
        if auth_header and self.check_auth_header(auth_header):
            handler.auth_header = auth_header
        else:
            headers = {'Access-Control-Allow-Origin': '*',
                       'Proxy-Authenticate': 'Basic realm="%s"' % self.auth_info,
                       'Content-Length': '0',
                       'Connection': 'keep-alive'}
            return {'status': 407, 'headers': headers, 'content': ''}


class GAEProxyHandler(simple_http_server.HttpServerHandler):
    gae_support_methods = tuple(["GET", "POST", "HEAD", "PUT", "DELETE", "PATCH"])
    bufsize = 256*1024
    max_retry = 3
    local_hosts = ['localhost', '127.0.0.1', config.get_listen_ip()]
    handler_filters=None

    if config.LISTEN_USERNAME:
        handler_filters = AuthFilter(config.LISTEN_USERNAME, config.LISTEN_PASSWORD)

    def setup(self):
        self.__class__.do_GET = self.__class__.do_METHOD
        self.__class__.do_PUT = self.__class__.do_METHOD
        self.__class__.do_POST = self.__class__.do_METHOD
        self.__class__.do_HEAD = self.__class__.do_METHOD
        self.__class__.do_DELETE = self.__class__.do_METHOD
        self.__class__.do_OPTIONS = self.__class__.do_METHOD

    def forward_local(self):
        """
        If browser send localhost:xxx request to GAE_proxy,
        we forward it to localhost.
        """
        host = self.headers.get('Host', '')
        host_ip, _, port = host.rpartition(':')
        http_client = simple_http_client.HTTP_client((host_ip, int(port)))

        request_headers = dict((k.title(), v) for k, v in self.headers.items())
        payload = b''
        if 'Content-Length' in request_headers:
            try:
                payload_len = int(request_headers.get('Content-Length', 0))
                payload = self.rfile.read(payload_len)
            except Exception as e:
                xlog.warn('forward_local read payload failed:%s', e)
                return

        self.parsed_url = urlparse.urlparse(self.path)
        if len(self.parsed_url[4]):
            path = '?'.join([self.parsed_url[2], self.parsed_url[4]])
        else:
            path = self.parsed_url[2]
        content, status, response = http_client.request(self.command, path, request_headers, payload)
        if not status:
            xlog.warn("forward_local fail")
            return

        out_list = []
        out_list.append("HTTP/1.1 %d\r\n" % status)
        for key, value in response.getheaders():
            key = key.title()
            out_list.append("%s: %s\r\n" % (key, value))
        out_list.append("\r\n")
        out_list.append(content)

        self.wfile.write("".join(out_list))

    def send_method_allows(self, headers, payload):
        xlog.debug("send method allow list for:%s %s", self.command, self.path)
        # Refer: https://developer.mozilla.org/en-US/docs/Web/HTTP/Access_control_CORS#Preflighted_requests

        response = \
                "HTTP/1.1 200 OK\r\n"\
                "Access-Control-Allow-Credentials: true\r\n"\
                "Access-Control-Allow-Methods: GET, POST, HEAD, PUT, DELETE, PATCH\r\n"\
                "Access-Control-Max-Age: 1728000\r\n"\
                "Content-Length: 0\r\n"

        req_header = headers.get("Access-Control-Request-Headers", "")
        if req_header:
            response += "Access-Control-Allow-Headers: %s\r\n" % req_header

        origin = headers.get("Origin", "")
        if origin:
            response += "Access-Control-Allow-Origin: %s\r\n" % origin
        else:
            response += "Access-Control-Allow-Origin: *\r\n"

        response += "\r\n"

        self.wfile.write(response)

    def is_local(self, host):
        for s in self.local_hosts:
            if host.startswith(s):
                return True
        return False

    def do_METHOD(self):
        touch_active()
        # record active time.
        # backgroud thread will stop keep connection pool if no request for long time.

        host = self.headers.get('Host', '')
        host_ip, _, port = host.rpartition(':')
        if host_ip in self.local_hosts and port == str(config.LISTEN_PORT):
            data = config.summary()
            self.wfile.write(('HTTP/1.1 200\r\nContent-Type: text/plain\r\nContent-Length: %s\r\n\r\n' % len(data)).encode())
            return self.wfile.write(data)

        if self.path[0] == '/' and host:
            self.path = 'http://%s%s' % (host, self.path)
        elif not host and '://' in self.path:
            host = urlparse.urlparse(self.path).netloc

        if self.is_local(host):
            xlog.info("Browse localhost by proxy")
            return self.forward_local()

        self.parsed_url = urlparse.urlparse(self.path)

        if host in config.HOSTS_GAE:
            return self.do_AGENT()

        # redirect http request to https request
        # avoid key word filter when pass through GFW
        if host in config.HOSTS_FWD or host in config.HOSTS_DIRECT:
            return self.wfile.write(('HTTP/1.1 301\r\nLocation: %s\r\nContent-Length: 0\r\n\r\n' % self.path.replace('http://', 'https://', 1)).encode())

        if host.endswith(config.HOSTS_GAE_ENDSWITH):
            return self.do_AGENT()

        if host.endswith(config.HOSTS_FWD_ENDSWITH) or host.endswith(config.HOSTS_DIRECT_ENDSWITH):
            return self.wfile.write(('HTTP/1.1 301\r\nLocation: %s\r\nContent-Length: 0\r\n\r\n' % self.path.replace('http://', 'https://', 1)).encode())

        return self.do_AGENT()

    # Called by do_METHOD and do_CONNECT_AGENT
    def do_AGENT(self):
        def get_crlf(rfile):
            crlf = rfile.readline(2)
            if crlf != "\r\n":
                xlog.warn("chunk header read fail crlf")

        request_headers = dict((k.title(), v) for k, v in self.headers.items())

        payload = b''
        if 'Content-Length' in request_headers:
            try:
                payload_len = int(request_headers.get('Content-Length', 0))
                #xlog.debug("payload_len:%d %s %s", payload_len, self.command, self.path)
                payload = self.rfile.read(payload_len)
            except NetWorkIOError as e:
                xlog.error('handle_method_urlfetch read payload failed:%s', e)
                return
        elif 'Transfer-Encoding' in request_headers:
            # chunked, used by facebook android client
            payload = ""
            while True:
                chunk_size_str = self.rfile.readline(65537)
                chunk_size_list = chunk_size_str.split(";")
                chunk_size = int("0x"+chunk_size_list[0], 0)
                if len(chunk_size_list) > 1 and chunk_size_list[1] != "\r\n":
                    xlog.warn("chunk ext: %s", chunk_size_str)
                if chunk_size == 0:
                    while True:
                        line = self.rfile.readline(65537)
                        if line == "\r\n":
                            break
                        else:
                            xlog.warn("entity header:%s", line)
                    break
                payload += self.rfile.read(chunk_size)
                get_crlf(self.rfile)

        if self.command == "OPTIONS":
            return self.send_method_allows(request_headers, payload)

        if self.command not in self.gae_support_methods:
            xlog.warn("Method %s not support in GAEProxy for %s", self.command, self.path)
            return self.wfile.write(('HTTP/1.1 404 Not Found\r\n\r\n').encode())

        xlog.debug("GAE %s %s", self.command, self.path)
        gae_handler.handler(self.command, self.path, request_headers, payload, self.wfile)

    def do_CONNECT(self):
        touch_active()

        host, _, port = self.path.rpartition(':')

        if host in config.HOSTS_GAE:
            return self.do_CONNECT_AGENT()
        if host in config.HOSTS_DIRECT:
            return self.do_CONNECT_DIRECT()

        if host.endswith(config.HOSTS_GAE_ENDSWITH):
            return self.do_CONNECT_AGENT()
        if host.endswith(config.HOSTS_DIRECT_ENDSWITH):
            return self.do_CONNECT_DIRECT()

        return self.do_CONNECT_AGENT()

    def do_CONNECT_AGENT(self):
        """send fake cert to client"""
        # GAE supports the following HTTP methods: GET, POST, HEAD, PUT, DELETE, and PATCH
        host, _, port = self.path.rpartition(':')
        port = int(port)
        certfile = CertUtil.get_cert(host)
        # xlog.info('https GAE %s %s:%d ', self.command, host, port)
        self.__realconnection = None
        self.wfile.write(b'HTTP/1.1 200 OK\r\n\r\n')

        try:
            ssl_sock = ssl.wrap_socket(self.connection, keyfile=certfile, certfile=certfile, server_side=True)
        except ssl.SSLError as e:
            xlog.info('ssl error: %s, create full domain cert for host:%s', e, host)
            certfile = CertUtil.get_cert(host, full_name=True)
            return
        except Exception as e:
            if e.args[0] not in (errno.ECONNABORTED, errno.ECONNRESET):
                xlog.exception('ssl.wrap_socket(self.connection=%r) failed: %s path:%s, errno:%s', self.connection, e, self.path, e.args[0])
            return

        self.__realconnection = self.connection
        self.__realwfile = self.wfile
        self.__realrfile = self.rfile
        self.connection = ssl_sock
        self.rfile = self.connection.makefile('rb', self.bufsize)
        self.wfile = self.connection.makefile('wb', 0)

        try:
            self.raw_requestline = self.rfile.readline(65537)
            if len(self.raw_requestline) > 65536:
                self.requestline = ''
                self.request_version = ''
                self.command = ''
                self.send_error(414)
                xlog.warn("read request line len:%d", len(self.raw_requestline))
                return
            if not self.raw_requestline:
                # xlog.warn("read request line empty")
                return
            if not self.parse_request():
                xlog.warn("parse request fail:%s", self.raw_requestline)
                return
        except NetWorkIOError as e:
            if e.args[0] not in (errno.ECONNABORTED, errno.ECONNRESET, errno.EPIPE):
                xlog.exception('ssl.wrap_socket(self.connection=%r) failed: %s path:%s, errno:%s', self.connection, e, self.path, e.args[0])
                raise
        if self.path[0] == '/' and host:
            self.path = 'https://%s%s' % (self.headers['Host'], self.path)

        try:
            if self.path[0] == '/' and host:
                self.path = 'http://%s%s' % (host, self.path)
            elif not host and '://' in self.path:
                host = urlparse.urlparse(self.path).netloc

            self.parsed_url = urlparse.urlparse(self.path)

            return self.do_AGENT()

        except NetWorkIOError as e:
            if e.args[0] not in (errno.ECONNABORTED, errno.ETIMEDOUT, errno.EPIPE):
                raise
        finally:
            if self.__realconnection:
                try:
                    self.__realconnection.shutdown(socket.SHUT_WR)
                    self.__realconnection.close()
                except NetWorkIOError:
                    pass
                finally:
                    self.__realconnection = None

    def do_CONNECT_DIRECT(self):
        """deploy fake cert to client"""
        host, _, port = self.path.rpartition(':')
        port = int(port)
        if port != 443:
            xlog.warn("CONNECT %s port:%d not support", host, port)
            return

        certfile = CertUtil.get_cert(host)
        xlog.info('GAE %s %s:%d ', self.command, host, port)
        self.__realconnection = None
        self.wfile.write(b'HTTP/1.1 200 OK\r\n\r\n')

        try:
            ssl_sock = ssl.wrap_socket(self.connection, keyfile=certfile, certfile=certfile, server_side=True)
        except ssl.SSLError as e:
            xlog.info('ssl error: %s, create full domain cert for host:%s', e, host)
            certfile = CertUtil.get_cert(host, full_name=True)
            return
        except Exception as e:
            if e.args[0] not in (errno.ECONNABORTED, errno.ECONNRESET):
                xlog.exception('ssl.wrap_socket(self.connection=%r) failed: %s path:%s, errno:%s', self.connection, e, self.path, e.args[0])
            return

        self.__realconnection = self.connection
        self.__realwfile = self.wfile
        self.__realrfile = self.rfile
        self.connection = ssl_sock
        self.rfile = self.connection.makefile('rb', self.bufsize)
        self.wfile = self.connection.makefile('wb', 0)

        try:
            self.raw_requestline = self.rfile.readline(65537)
            if len(self.raw_requestline) > 65536:
                self.requestline = ''
                self.request_version = ''
                self.command = ''
                self.send_error(414)
                return
            if not self.raw_requestline:
                self.close_connection = 1
                return
            if not self.parse_request():
                return
        except NetWorkIOError as e:
            if e.args[0] not in (errno.ECONNABORTED, errno.ECONNRESET, errno.EPIPE):
                raise
        if self.path[0] == '/' and host:
            self.path = 'https://%s%s' % (self.headers['Host'], self.path)

        xlog.debug('GAE CONNECT Direct %s %s', self.command, self.path)

        try:
            if self.path[0] == '/' and host:
                self.path = 'http://%s%s' % (host, self.path)
            elif not host and '://' in self.path:
                host = urlparse.urlparse(self.path).netloc

            self.parsed_url = urlparse.urlparse(self.path)
            if len(self.parsed_url[4]):
                path = '?'.join([self.parsed_url[2], self.parsed_url[4]])
            else:
                path = self.parsed_url[2]

            request_headers = dict((k.title(), v) for k, v in self.headers.items())

            payload = b''
            if 'Content-Length' in request_headers:
                try:
                    payload_len = int(request_headers.get('Content-Length', 0))
                    #xlog.debug("payload_len:%d %s %s", payload_len, self.command, self.path)
                    payload = self.rfile.read(payload_len)
                except NetWorkIOError as e:
                    xlog.error('handle_method_urlfetch read payload failed:%s', e)
                    return

            direct_handler.handler(self.command, host, path, request_headers, payload, self.wfile)

        except NetWorkIOError as e:
            if e.args[0] not in (errno.ECONNABORTED, errno.ETIMEDOUT, errno.EPIPE):
                raise
        finally:
            if self.__realconnection:
                try:
                    self.__realconnection.shutdown(socket.SHUT_WR)
                    self.__realconnection.close()
                except NetWorkIOError:
                    pass
                finally:
                    self.__realconnection = None

