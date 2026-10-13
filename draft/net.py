"""Единый HTTP-слой с поддержкой прокси/VPN.

Приложение работает с опенсорс-сайтами, часть из которых отвечает только
через VPN (Cloudflare-403 без него) или наоборот тормозит через него. В
настройках задаётся адрес вида http://127.0.0.1:10808 (порт бывает 10809);
пустая строка = прямое соединение. Настройка общая для всех источников
(op.gg, u.gg, lolalytics), чтобы не дублировать её в каждом парсере.
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")

_proxy: str = ""


def set_proxy(url: str) -> None:
    """Задать (или снять пустой строкой) прокси для всех запросов."""
    global _proxy
    url = (url or "").strip()
    if url and "://" not in url:
        url = "http://" + url
    _proxy = url


def proxy() -> str:
    return _proxy


class NetError(RuntimeError):
    """Источник не ответил: нет сети, изменилась вёрстка, таймаут."""


def _build_opener():
    if _proxy:
        handler = urllib.request.ProxyHandler({"http": _proxy,
                                               "https": _proxy})
        return urllib.request.build_opener(handler)
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def request(url: str, *, accept: str = "application/json",
            timeout: int = 30, retries: int = 2,
            payload: dict | None = None) -> str:
    """GET (или POST с JSON-телом) через текущий прокени.

    Ошибки оборачиваем в NetError с человекочитаемым текстом: вызывающий
    код показывает его в логе/футере, а не роняет синк трейсбеком.
    """
    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            headers = {"User-Agent": UA, "Accept": accept,
                       "Accept-Language": "en-US,en;q=0.9",
                       "Connection": "close"}
            data = None
            if payload is not None:
                data = json.dumps(payload).encode()
                headers["Content-Type"] = "application/json"
            req = urllib.request.Request(url, headers=headers, data=data)
            with _build_opener().open(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:                        # noqa: BLE001
            last = e
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
    raise NetError(f"{url}: {type(last).__name__}: {last}")


def get_html(url: str, **kw) -> str:
    return request(url, accept="text/html,application/xhtml+xml", **kw)


def get_json(url: str, **kw):
    import json as _json
    return _json.loads(request(url, **kw))


def post_json(url: str, payload: dict, **kw):
    return request(url, payload=payload, **kw)
