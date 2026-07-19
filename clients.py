import json
import urllib.error
import urllib.parse
import urllib.request
import http.cookiejar


class QbClient:
    def __init__(self, api_base, username, password, logger):
        self.api_base = api_base.rstrip("/")
        self.username = username
        self.password = password
        self.log = logger
        self.cookiejar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cookiejar)
        )

    def qb_url(self, path):
        if self.api_base.endswith("/api/v2"):
            return f"{self.api_base}{path}"
        return f"{self.api_base}/api/v2{path}"

    def login(self):
        data = urllib.parse.urlencode(
            {"username": self.username, "password": self.password}
        ).encode()
        req = urllib.request.Request(self.qb_url("/auth/login"), data=data, method="POST")

        with self.opener.open(req, timeout=5) as resp:
            body = resp.read().decode().strip()
            if resp.status != 200 or body != "Ok.":
                raise RuntimeError(
                    f"Failed to login to qBittorrent API status:{resp.status}, body:{body}"
                )

        self.log.info("Successfully logged in to qBittorrent API")

    def with_reauth(self, func, *args, **kwargs):
        try:
            return func(*args, **kwargs)
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                self.log.info("qBittorrent session expired, logging in again")
                self.login()
                return func(*args, **kwargs)
            raise

    def get_torrent_list(self):
        req = urllib.request.Request(self.qb_url("/torrents/info?filter=completed"))
        try:
            with self.opener.open(req, timeout=5) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise

            self.log.warning("Failed to connect to qBittorrent API with status %s", e.code)
            try:
                body = e.read().decode(errors="replace")
                self.log.warning("Response body: %s", body)
            except Exception:
                pass
            return None
        except (urllib.error.URLError, json.JSONDecodeError, OSError) as e:
            self.log.warning("Error fetching torrent list from qBittorrent: %s", e)
            return None

    def get_torrent_properties(self, torrent_hash):
        req = urllib.request.Request(self.qb_url(f"/torrents/properties?hash={torrent_hash}"))
        with self.opener.open(req, timeout=5) as resp:
            return json.loads(resp.read().decode())

    def delete_torrent(self, torrent_hash, delete_files):
        data = urllib.parse.urlencode({"hashes": torrent_hash}).encode()
        delete_path = "/torrents/deletePerm" if delete_files else "/torrents/delete"
        req = urllib.request.Request(self.qb_url(delete_path), data=data, method="POST")
        with self.opener.open(req, timeout=10) as resp:
            if resp.status != 200:
                raise RuntimeError(
                    f"Failed deleting torrent {torrent_hash}: status {resp.status}"
                )


class ArrClient:
    def __init__(self, base_url, api_key):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def is_configured(self):
        return bool(self.base_url and self.api_key)

    def service_url(self, path):
        if self.base_url.endswith("/api/v3"):
            return f"{self.base_url}{path}"
        return f"{self.base_url}/api/v3{path}"

    def fetch_json(self, path):
        req = urllib.request.Request(self.service_url(path))
        req.add_header("X-Api-Key", self.api_key)
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
