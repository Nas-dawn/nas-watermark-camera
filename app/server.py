#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""水印相机 v2 (2026-09-07)：左下角水印卡 + 注册登录 + NAS 相册访问
端口 8335；照片存 /fs/1000/ftp/DawnAI输出/水印相机照片/<日期>/
"""
import json, os, re, time, math, base64, hmac, hashlib, secrets, threading, urllib.request, datetime, random, string
from PIL import Image, ImageDraw, ImageFont
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

BASE = os.path.dirname(os.path.abspath(__file__))
BRAND_CN = "示例建筑"           # 拼图汇报长图上的品牌角标，可改
SAVE_ROOT = "/fs/1000/ftp/DawnAI输出/水印相机照片"
# v46：删除 = 移到回收站（不物理抹掉，可在 NAS 上找回）
TRASH_ROOT = os.path.join(os.path.dirname(SAVE_ROOT), "水印相机回收站")
USERS = os.path.join(BASE, "users.json")
PREFS = os.path.join(BASE, "prefs.json")
SECRET = "guoding_watercam_2026_h1"
COOKIE = "wc_auth"
COOKIE_MAXAGE = 315360000      # 10 年：登录一次，之后除非换手机/清浏览器数据，不用再登录
TOKEN_DAYS = 3650              # token 本身也签到 10 年后
WEATHER_CITY = "Wuxue"
WEATHER_CITY_CN = "北京"          # 无定位时的天气兜底城市（可改）
CACHE_TTL = 600
_weather_cache = {"t": 0, "data": None, "key": ""}
_lock = threading.Lock()

WCODE_ZH = {
    "113": "晴", "116": "多云", "119": "阴", "122": "阴", "143": "雾", "248": "雾", "260": "冻雾",
    "176": "小雨", "263": "小雨", "266": "小雨", "293": "小雨", "296": "小雨", "353": "小雨",
    "299": "中雨", "302": "中雨", "356": "中雨", "305": "大雨", "308": "大雨", "359": "大雨",
    "200": "雷阵雨", "386": "雷阵雨", "389": "雷阵雨",
    "179": "小雪", "227": "小雪", "323": "小雪", "326": "小雪", "368": "小雪",
    "329": "中雪", "332": "中雪", "335": "大雪", "338": "大雪", "371": "大雪", "395": "大雪",
    "182": "雨夹雪", "317": "雨夹雪", "320": "雨夹雪", "362": "雨夹雪", "365": "雨夹雪",
    "350": "冰粒", "374": "冰粒", "377": "冰粒", "420": "扬沙", "421": "扬沙",
}
WINDS = {"N":"北","NNE":"北东北","NE":"东北","ENE":"东北东","E":"东","ESE":"东南东","SE":"东南","SSE":"南东南",
         "S":"南","SSW":"南西南","SW":"西南","WSW":"西南西","W":"西","WNW":"西北西","NW":"西北","NNW":"北西北"}

def _zh_desc(code): return WCODE_ZH.get(str(code), "")
def _wind_level(kmh):
    try: v = float(kmh)
    except: return "?"
    if v < 1: return 0
    if v < 6: return 1
    if v < 12: return 2
    if v < 20: return 3
    if v < 29: return 4
    if v < 39: return 5
    if v < 50: return 6
    if v < 62: return 7
    return 8

# ---------- 用户 ----------
def load_prefs():
    try:
        with open(PREFS, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_prefs(d):
    """原子写：先写临时文件再改名，避免半个文件把配置读坏"""
    try:
        tmp = PREFS + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
        os.replace(tmp, PREFS)
        try:
            os.chmod(PREFS, 0o666)
        except Exception:
            pass
        return True
    except Exception:
        return False


def load_users():
    if not os.path.isfile(USERS):
        return {}
    try:
        return json.load(open(USERS, encoding="utf-8"))
    except Exception:
        return {}

def save_users(u):
    tmp = USERS + ".tmp"
    json.dump(u, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    os.replace(tmp, USERS)

def hash_pw(pw, salt=None):
    salt = salt or secrets.token_hex(8)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 60000).hex()
    return salt + "$" + h

def check_pw(pw, stored):
    try:
        salt, h = stored.split("$")
        return secrets.compare_digest(hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 60000).hex(), h)
    except Exception:
        return False

def make_token(user):
    exp = int(time.time()) + TOKEN_DAYS * 86400
    payload = user + ":" + str(exp)
    sig = hmac.new(SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()[:32]
    return payload + "." + sig

def check_token(tok):
    try:
        payload, sig = tok.split(".")
        expect = hmac.new(SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()[:32]
        if not hmac.compare_digest(sig, expect):
            return None
        user, exp = payload.rsplit(":", 1)
        if int(exp) < time.time():
            return None
        return user
    except Exception:
        return None

# ---------- 天气 / 定位 ----------
def get_weather(lat=None, lng=None):
    global _weather_cache
    now = time.time()
    key = ("%.3f,%.3f" % (float(lat), float(lng))) if (lat and lng) else WEATHER_CITY
    if _weather_cache["data"] and _weather_cache["key"] == key and now - _weather_cache["t"] < CACHE_TTL:
        return _weather_cache["data"]
    out = {"ok": False, "city": WEATHER_CITY_CN}
    try:
        req = urllib.request.Request("https://wttr.in/" + key + "?format=j1&lang=zh",
                                     headers={"User-Agent": "Mozilla/5.0 (watermark-cam)"})
        j = json.loads(urllib.request.urlopen(req, timeout=12).read().decode("utf-8", "replace"))
        cur = j["current_condition"][0]
        desc = _zh_desc(cur.get("weatherCode", "")) or (cur.get("weatherDesc") or [{"value": ""}])[0].get("value", "")
        wd = cur.get("winddir16Point") or ""
        out = {"ok": True, "city": WEATHER_CITY_CN, "text": desc or "—", "temp": cur.get("temp_C", "—"),
               "wind": "%s风%s级" % (WINDS.get(wd.upper(), wd), _wind_level(cur.get("windspeedKmph", 0))),
               "humidity": cur.get("humidity", "")}
        _weather_cache = {"t": now, "key": key, "data": out}
    except Exception as e:
        out["err"] = str(e)[:120]
    return out

# 高德 Web服务 Key（可选项，用于定位反查地名+天气；不填则地点降级为经纬度）
# 申请：https://console.amap.com/dev/key/app （个人免费 5000 次/日）
# 配置：环境变量 GAODE_KEY，或在本文件同目录放 amap_key.txt（只写 key 一行）
def _load_amap_key():
    k = (os.environ.get("GAODE_KEY") or "").strip()
    if k:
        return k
    try:
        _p = os.path.join(BASE, "amap_key.txt")
        if os.path.isfile(_p):
            return open(_p, encoding="utf-8").read().strip()
    except Exception:
        pass
    return ""


GAODE_KEY = _load_amap_key()

# ---- WGS84 -> GCJ02（高德坐标加密偏移，手机 GPS 必须转后才准） ----
def _out_of_china(lat, lng):
    return not (72.004 <= lng <= 137.8347 and 0.8293 <= lat <= 55.8271)

def wgs2gcj(lat, lng):
    if _out_of_china(lat, lng):
        return lat, lng
    a, ee = 6378245.0, 0.00669342162296594323
    dlat = _tlat(lng - 105.0, lat - 35.0)
    dlng = _tlng(lng - 105.0, lat - 35.0)
    rad = lat / 180.0 * 3.141592653589793
    magic = 1 - ee * math.sin(rad) ** 2
    m = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((a * (1 - ee)) / (magic * m) * 3.141592653589793)
    dlng = (dlng * 180.0) / (a / m * math.cos(rad) * 3.141592653589793)
    return lat + dlat, lng + dlng

def _tlat(x, y):
    r = -100.0 + 2.0 * x + 3.0 * y + 0.2 * y * y + 0.1 * x * y + 0.2 * math.sqrt(abs(x))
    r += (20.0 * math.sin(6.0 * x * 3.141592653589793) + 20.0 * math.sin(2.0 * x * 3.141592653589793)) * 2.0 / 3.0
    r += (20.0 * math.sin(y * 3.141592653589793) + 40.0 * math.sin(y / 3.0 * 3.141592653589793)) * 2.0 / 3.0
    r += (160.0 * math.sin(y / 12.0 * 3.141592653589793) + 320 * math.sin(y * 3.141592653589793 / 30.0)) * 2.0 / 3.0
    return r

def _tlng(x, y):
    r = 300.0 + x + 2.0 * y + 0.1 * x * x + 0.1 * x * y + 0.1 * math.sqrt(abs(x))
    r += (20.0 * math.sin(6.0 * x * 3.141592653589793) + 20.0 * math.sin(2.0 * x * 3.141592653589793)) * 2.0 / 3.0
    r += (20.0 * math.sin(x * 3.141592653589793) + 40.0 * math.sin(x / 3.0 * 3.141592653589793)) * 2.0 / 3.0
    r += (150.0 * math.sin(x / 12.0 * 3.141592653589793) + 300.0 * math.sin(x / 30.0 * 3.141592653589793)) * 2.0 / 3.0
    return r

def regeo(lat, lng):
    # 首选高德：可到乡镇/村（格式：黄冈市·杨以忠）——必须 WGS84→GCJ02 转换
    try:
        glat, glng = wgs2gcj(float(lat), float(lng))
        url = ("https://restapi.amap.com/v3/geocode/regeo?key=" + GAODE_KEY +
               "&location=%.6f,%.6f&extensions=base" % (glng, glat))
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (watermark-cam)"})
        j = json.loads(urllib.request.urlopen(req, timeout=10).read().decode("utf-8", "replace"))
        if j.get("status") == "1":
            rg = j["regeocode"] or {}
            ac = rg.get("addressComponent") or {}
            prov = ac.get("province") or ""
            city = (ac.get("city") or "").replace("市", "")
            dist = (ac.get("district") or "").replace("市", "")
            town = ac.get("township") or ""
            fa = rg.get("formatted_address") or ""
            rest = fa
            for pre in (prov, (city + "市"), (dist + "市"), town):
                if rest.startswith(pre):
                    rest = rest[len(pre):]
            rest = rest.strip()
            if rest:
                return {"ok": True, "label": city + "市·" + rest}
            if town:
                return {"ok": True, "label": city + "市·" + town}
            return {"ok": True, "label": (city + "市") if city else (dist + "市")}
    except Exception:
        pass
    # 备用：bigdatacloud（WGS84，仅到县市级）
    try:
        url = ("https://api.bigdatacloud.net/data/reverse-geocode-client?latitude=%s&longitude=%s"
               "&localityLanguage=zh" % (lat, lng))
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (watermark-cam)"})
        j = json.loads(urllib.request.urlopen(req, timeout=10).read().decode("utf-8", "replace"))
        city = (j.get("city") or "").replace("市", "")
        loc = (j.get("locality") or "").replace("市", "")
        label = (city + "市·" + loc) if (loc and loc != city) else (city + "市" if city else (j.get("principalSubdivision") or ("GPS:" + lat + "," + lng)))
        return {"ok": True, "label": label}
    except Exception:
        return {"ok": False}

# ---------- 相册 ----------
TRASH_KEEP_DAYS = 30          # v46d：回收站保留天数（用户定），超期自动清


def purge_trash(verbose=False):
    # 清掉回收站里超过 TRASH_KEEP_DAYS 的文件（按 mtime = 删除时刻，见 /api/delete 的 os.utime）
    n = 0
    if not os.path.isdir(TRASH_ROOT):
        return 0
    lim = TRASH_KEEP_DAYS * 86400
    now = time.time()
    for root, _dirs, files in os.walk(TRASH_ROOT):
        for fn in files:
            fp = os.path.join(root, fn)
            try:
                if now - os.path.getmtime(fp) > lim:
                    os.remove(fp)
                    n += 1
            except Exception:
                pass
    for root, _dirs, _files in os.walk(TRASH_ROOT, topdown=False):   # 顺手清掉空目录
        if os.path.realpath(root) == os.path.realpath(TRASH_ROOT):
            continue
        try:
            if not os.listdir(root):
                os.rmdir(root)
        except Exception:
            pass
    if verbose and n:
        print("[trash] 清理超期文件 %d 个" % n)
    return n


def _trash_worker():
    while True:
        try:
            purge_trash(verbose=True)
        except Exception:
            pass
        time.sleep(6 * 3600)      # 每 6 小时扫一次


def list_photos(day=None, q=None, me_name=None):
    # 目录结构：SAVE_ROOT/<记录人>/<日期>/file  —— 按人分夹，但列表按日期聚合（所有人可见）
    byday = {}
    if os.path.isdir(SAVE_ROOT):
        for user_dir in sorted(os.listdir(SAVE_ROOT), reverse=True):
            up = os.path.join(SAVE_ROOT, user_dir)
            if not os.path.isdir(up):
                continue
            for d in sorted(os.listdir(up), reverse=True):
                if not re.match(r"^\d{4}-\d{2}-\d{2}$", d):
                    continue
                if day and d != day:
                    continue
                fp = os.path.join(up, d)
                if not os.path.isdir(fp):
                    continue
                for fn in sorted(os.listdir(fp), reverse=True):
                    if not re.match(r"^[\w.\-]+\.(jpg|jpeg|png|mp4|webm)$", fn, re.I):
                        continue
                    p = os.path.join(fp, fn)
                    meta = None
                    txt = os.path.splitext(p)[0] + ".txt"
                    if os.path.isfile(txt):
                        meta = {}
                        for ln in open(txt, encoding="utf-8"):
                            if "：" in ln:
                                k, v = ln.strip().split("：", 1)
                                meta[k] = v
                    if q:
                        hay = " ".join([user_dir, d, fn] + list((meta or {}).keys()) +
                                       [str(v) for v in (meta or {}).values()]).lower()
                        if q.lower() not in hay:
                            continue
                    _rec = ((meta or {}).get("记录人") or "").strip()
                    _mine = bool(me_name) and ((_rec == me_name) or ((not _rec) and (user_dir == me_name)))
                    byday.setdefault(d, []).append({"name": fn, "size": os.path.getsize(p), "meta": meta,
                                                    "owner": user_dir, "mine": _mine})
    days = [{"day": d, "files": byday[d]} for d in sorted(byday, reverse=True)]
    return days



# ==================== 拼图汇报（选多张照片 → 一张长图，供发群/存档） ====================
FONT_PATH = os.path.join(BASE, "fonts", "NotoSansSC.otf")
_font_cache = {}


def _font(size):
    if size not in _font_cache:
        try:
            _font_cache[size] = ImageFont.truetype(FONT_PATH, size)
        except Exception:
            _font_cache[size] = ImageFont.load_default()
    return _font_cache[size]


def _text_w(d, t, f):
    return d.textlength(t, font=f)


def _fit(d, t, f, maxw):
    t = str(t or "")
    if _text_w(d, t, f) <= maxw:
        return t
    while t and _text_w(d, t + "…", f) > maxw:
        t = t[:-1]
    return t + "…"


def _cover(im, w, h):
    """等比裁切填满 w×h（居中）"""
    r = max(w / im.width, h / im.height)
    im2 = im.resize((max(1, int(im.width * r + .5)), max(1, int(im.height * r + .5))), Image.LANCZOS)
    left = (im2.width - w) // 2
    top = (im2.height - h) // 2
    return im2.crop((left, top, left + w, top + h))


def _round(im, r):
    mask = Image.new("L", im.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, im.width - 1, im.height - 1], radius=r, fill=255)
    out = im.convert("RGB").copy()
    out.putalpha(mask)
    return out


def build_report(items, layout, title, who, user):
    """items=[{owner,day,name}...]  layout: 2 / 3 / big；返回保存后的相对路径"""
    W = 1080
    pad, gap = 26, 20
    cols = 3 if layout == "3" else 2
    big = (layout == "big")
    cw = (W - 2 * pad - gap * (cols - 1)) // cols
    cimg_h = int(cw * 3 / 4)
    cap_h = 62
    head_h = 196
    foot_h = 168

    # 先算总高
    rows = []
    rest = list(items)
    if big and rest:
        rows.append(("big", rest.pop(0)))
    while rest:
        rows.append(("row", rest[:cols]))
        rest = rest[cols:]
    H = head_h + sum((580 if k == "big" else cimg_h + cap_h) + gap for k, _ in rows) - gap + foot_h
    canvas = Image.new("RGB", (W, H), "#ffffff")
    d = ImageDraw.Draw(canvas)

    # 页眉
    d.rectangle([0, 0, W, head_h], fill="#12306e")
    d.rounded_rectangle([pad, 44, pad + 76, 120], radius=16, fill="#1e4fa3")
    d.text((pad + 18, 54), BRAND_CN, font=_font(30), fill="#ffffff")
    d.text((pad + 100, 46), _fit(d, title or "施工影像汇报", _font(44), W - pad * 2 - 100), font=_font(44), fill="#ffffff")
    sub = "%s　|　记录人：%s" % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), who or "-")
    d.text((pad + 100, 108), sub, font=_font(26), fill="#bcd2f0")
    d.text((pad, 148), "共 %d 张照片" % len(items), font=_font(24), fill="#8fb0dd")

    # 图片区
    y = head_h
    for kind, payload in rows:
        if kind == "big":
            it = payload
            bw = W - pad * 2
            bh = 580
            _draw_cell(canvas, d, it, pad, y, bw, bh, cap_h, big=True)
            y += bh + cap_h + gap
        else:
            n = len(payload)
            row_w = n * cw + (n - 1) * gap
            x0 = pad + (W - 2 * pad - row_w) // 2 if n < cols else pad   # 不满一行时居中，不留右下大片空白
            for i, it in enumerate(payload):
                _draw_cell(canvas, d, it, x0 + i * (cw + gap), y, cw, cimg_h, cap_h)
            y += cimg_h + cap_h + gap

    # 页脚
    fy = H - foot_h
    d.rectangle([0, fy, W, H], fill="#eef2f7")
    d.text((pad, fy + 26), "施工单位：示例建设工程有限公司", font=_font(26), fill="#334155")
    d.text((pad, fy + 66), "监理单位：示例监理咨询有限公司", font=_font(26), fill="#334155")
    d.text((pad, fy + 110), BRAND_CN + " · 水印相机自动生成　%s" % datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
           font=_font(22), fill="#7c8aa0")

    now = datetime.datetime.now()
    us = load_users()
    who_full = (us.get(user) or {}).get("name", user) or "未命名"
    safe = re.sub(r'[\\/:*?"<>|\s]', "_", who_full).strip() or "未命名"
    day = now.strftime("%Y-%m-%d")
    folder = os.path.join(SAVE_ROOT, safe, day)
    os.makedirs(folder, exist_ok=True)
    try:
        os.chmod(folder, 0o777)
    except Exception:
        pass
    name = "汇报_%s_%s.jpg" % (now.strftime("%Y%m%d_%H%M%S"), "".join(random.choices(string.ascii_lowercase + string.digits, k=3)))
    path = os.path.join(folder, name)
    canvas.save(path, "JPEG", quality=88, optimize=True)
    with open(os.path.splitext(path)[0] + ".txt", "w", encoding="utf-8") as f:
        f.write("水印相机记录\n类型：汇报图\n记录人：%s\n拍摄时间：%s\n施工区域：%s\n施工内容：%s\n" %
                (who_full, now.strftime("%Y-%m-%d %H:%M"), title or "", "共%d张" % len(items)))
    return "DawnAI输出/水印相机照片/%s/%s/%s" % (safe, day, name)


def _draw_cell(canvas, d, it, x, y, w, h, cap_h, big=False):
    """画一张照片 + 说明条"""
    name = it.get("name", "")
    meta = it.get("meta") or {}
    src = os.path.join(SAVE_ROOT, it.get("owner", ""), it.get("day", ""), name)
    try:
        im = Image.open(src)
        im = _cover(im, w, h)
        im = _round(im, 18)
        canvas.paste(im, (x, y), im)
    except Exception:
        d.rounded_rectangle([x, y, x + w, y + h], radius=18, fill="#dfe5ec")
        d.text((x + 20, y + h // 2 - 16), "图片读取失败", font=_font(26), fill="#8494a8")
    # 说明条
    cy = y + h + 6
    cap1 = " ".join([p for p in [meta.get("拍摄时间", ""), meta.get("施工区域", "")] if p])
    cap2 = " ".join([p for p in [meta.get("施工内容", ""), (meta.get("地点", "") or "").split("·")[-1]] if p])
    d.text((x + 6, cy), _fit(d, cap1 or name, _font(28 if big else 26), w - 12), font=_font(28 if big else 26), fill="#1f2d3d")
    d.text((x + 6, cy + 32), _fit(d, cap2, _font(22), w - 12), font=_font(22), fill="#6b7a8d")


REPORT_HTML = """<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>拼图汇报</title><style>
body{font-family:-apple-system,"PingFang SC",sans-serif;background:#eef1f5;margin:0;padding:12px 12px 120px}
.top{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px}
h2{margin:0;font-size:17px;color:#1a3d6d}
a{color:#1a5c8f;text-decoration:none;font-size:13px}
.card{background:#fff;border-radius:12px;padding:12px;margin-bottom:12px;box-shadow:0 1px 5px rgba(0,0,0,.05)}
.row{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.chip{padding:8px 14px;border-radius:999px;background:#f2f4f7;color:#475467;font-size:13px;font-weight:600}
.chip.on{background:#1d5bd8;color:#fff}
.pick{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}
.pk{position:relative;border-radius:10px;overflow:hidden;border:2px solid #e4e7ec}
.pk.on{border-color:#1d5bd8}
.pk img{width:100%;height:96px;object-fit:cover;display:block}
.pk .ck{position:absolute;right:6px;top:6px;width:24px;height:24px;border-radius:50%;background:rgba(16,24,40,.55);color:#fff;font-size:14px;display:flex;align-items:center;justify-content:center}
.pk.on .ck{background:#1d5bd8}
.pk .t{font-size:10.5px;color:#667085;padding:4px 6px;background:#fff;line-height:1.4}
input{width:100%;padding:11px;border:1px solid #e4e7ec;border-radius:10px;font-size:15px;box-sizing:border-box;margin-top:6px}
label{font-size:12px;color:#667085;font-weight:600}
.bar{position:fixed;left:0;right:0;bottom:0;background:#fff;border-top:1px solid #e4e7ec;padding:10px 12px calc(12px + env(safe-area-inset-bottom));display:flex;gap:10px}
.bar button{flex:1;padding:14px;border:none;border-radius:12px;background:#1d5bd8;color:#fff;font-size:16px;font-weight:700}
.bar button:disabled{background:#b9c6dc}
.hint{font-size:12px;color:#98a2b3;margin-top:8px}
#out{display:none;text-align:center}
#out img{width:100%;border-radius:12px;box-shadow:0 2px 12px rgba(0,0,0,.12)}
</style></head><body>
<div class="top"><h2>🧩 拼图汇报</h2><div><a href="/album">← 相册</a>　<a href="javascript:logout()">退出</a></div></div>

<div class="card">
  <div class="row" id="dayRow"></div>
  <div class="hint" id="cnt">已选 0 / 9 张（最多 9 张）</div>
</div>

<div class="card">
  <div class="row" id="layRow">
    <div class="chip on" data-l="2">两列（每行2张）</div>
    <div class="chip" data-l="3">三列（每行3张）</div>
    <div class="chip" data-l="big">首图放大 + 两列</div>
  </div>
</div>

<div class="card">
  <label>汇报标题</label><input id="title" value="施工影像汇报">
  <label>记录人 / 汇报人</label><input id="who" placeholder="如：张三">
</div>

<div id="box"><div class="hint">加载中…</div></div>

<div id="out"><div class="hint" style="margin:10px 0 6px">已生成，长按图片可保存 / 发给群里</div><img id="outImg"></div>

<div class="bar"><button id="go" disabled onclick="go()">生成汇报图</button></div>
<script>
let days=[], sel=[], lay="2";
function logout(){ document.cookie='wc_auth=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/'; location.href='/login'; }
async function load(){
  const r=await fetch('/api/photos'); const j=await r.json();
  days=j.days||[];
  const dr=document.getElementById('dayRow'); dr.innerHTML='';
  const all=document.createElement('div'); all.className='chip on'; all.textContent='全部日期';
  all.onclick=()=>{ document.querySelectorAll('#dayRow .chip').forEach(c=>c.classList.remove('on')); all.classList.add('on'); render(null); };
  dr.appendChild(all);
  days.slice(0,7).forEach(d=>{
    const c=document.createElement('div'); c.className='chip'; c.textContent=d.day;
    c.onclick=()=>{ document.querySelectorAll('#dayRow .chip').forEach(x=>x.classList.remove('on')); c.classList.add('on'); render(d.day); };
    dr.appendChild(c);
  });
  render(null);
}
function render(day){
  const box=document.getElementById('box'); box.innerHTML='';
  const list=days.filter(d=>!day||d.day===day).flatMap(d=>d.files.filter(f=>!/\.(mp4|webm)$/i.test(f.name)).map(f=>({...f,day:d.day})));
  if(!list.length){ box.innerHTML='<div class="hint">这天还没有照片</div>'; return; }
  const card=document.createElement('div'); card.className='card'; card.innerHTML='<div class="pick"></div>';
  const g=card.querySelector('.pick');
  list.slice(0,60).forEach(f=>{
    const m=f.meta||{};
    const el=document.createElement('div'); el.className='pk'; el.dataset.id=f.owner+'|'+f.day+'|'+f.name;
    el.innerHTML='<img src="/photo/'+f.owner+'/'+f.day+'/'+f.name+'"><div class="ck">✓</div><div class="t">'+((m['拍摄时间']||'').slice(5)+' '+(m['施工区域']||'')).trim()+'</div>';
    el.onclick=()=>{
      const id=el.dataset.id, i=sel.indexOf(id);
      if(i>=0){ sel.splice(i,1); el.classList.remove('on'); }
      else { if(sel.length>=9){ return; } sel.push(id); el.classList.add('on'); }
      upd();
    };
    g.appendChild(el);
  });
  box.appendChild(card);
}
function upd(){
  document.getElementById('cnt').textContent='已选 '+sel.length+' / 9 张'+(sel.length?'（按选择顺序排列）':'');
  document.getElementById('go').disabled=sel.length===0;
}
document.querySelectorAll('#layRow .chip').forEach(c=>c.onclick=()=>{
  document.querySelectorAll('#layRow .chip').forEach(x=>x.classList.remove('on')); c.classList.add('on'); lay=c.dataset.l; });
async function go(){
  const g=document.getElementById('go'); g.disabled=true; g.textContent='生成中…';
  const items=sel.map(s=>{ const [owner,day,name]=s.split('|'); return {owner,day,name}; });
  const body={files:items, layout:lay, title:document.getElementById('title').value.trim(), who:document.getElementById('who').value.trim()};
  const r=await fetch('/api/report',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
  const j=await r.json();
  g.textContent='生成汇报图'; g.disabled=false;
  if(j.ok){ const o=document.getElementById('out'); o.style.display='block';
    document.getElementById('outImg').src='/photo/'+j.rel.split('/').slice(2).join('/');
    window.scrollTo({top:document.body.scrollHeight,behavior:'smooth'}); }
  else { alert('生成失败：'+(j.err||'未知错误')); }
}
load(); upd();
</script></body></html>"""


def _html_page(title, body):
    return ("<!DOCTYPE html><html lang='zh'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>" + title + "</title></head><body style='font-family:sans-serif;background:#eef1f5;margin:0'>"
            + body + "</body></html>")

LOGIN_HTML = """<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="theme-color" content="#1a5c8f"><link rel="manifest" href="/manifest.json"><link rel="apple-touch-icon" href="/icons/apple-touch-icon.png"><meta name="apple-mobile-web-app-capable" content="yes"><meta name="apple-mobile-web-app-status-bar-style" content="black-translucent"><title>水印相机 · 登录</title><style>
body{font-family:sans-serif;background:#eef1f5;display:flex;align-items:center;justify-content:center;height:100vh;margin:0}
.card{background:#fff;border-radius:14px;padding:26px;width:300px;box-shadow:0 2px 10px rgba(0,0,0,.08)}
h2{text-align:center;color:#1a3d6d;margin:0 0 4px} .sub{font-size:12px;color:#999;text-align:center;margin-bottom:16px}
input{width:100%;padding:11px;border:1px solid #ddd;border-radius:8px;font-size:15px;margin:8px 0;box-sizing:border-box}
button{width:100%;padding:12px;border:none;border-radius:8px;background:#1a5c8f;color:#fff;font-size:16px;margin-top:6px}
.err{color:#d93025;font-size:13px;margin-top:8px;display:none;text-align:center}
.link{text-align:center;font-size:13px;margin-top:12px} a{color:#1a5c8f;text-decoration:none}
</style></head><body><a id="backHome" href="/" style="position:fixed;top:14px;left:14px;font-size:14px;color:#1a5c8f;text-decoration:none;background:#fff;padding:8px 12px;border-radius:8px;box-shadow:0 1px 6px rgba(0,0,0,.08);z-index:9">← 返回系统首页</a><script>(function(){var p=location.pathname;var a=document.getElementById('backHome');a.href=(p==='/wc'||p.indexOf('/wc/')===0)?'/':'https://bydawn.cpolar.cn/';})();</script>
<div class="card">
<h2>📷 水印相机</h2><div class="sub">用户名 = 注册时第2栏（不是姓名）<br>忘记用户名密码请联系管理员重置</div>
<input id="user" placeholder="用户名"><input type="password" id="pw" placeholder="密码" onkeydown="if(event.key=='Enter')login()">
<button onclick="login()">登 录</button><div class="err" id="err">用户名或密码错误</div>
<div class="link"><a href="/register">没有账号？去注册</a></div></div>
<script>
async function login(){
 const r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({user:document.getElementById('user').value.trim(),pw:document.getElementById('pw').value})});
 const j=await r.json();
 if(j.ok) location.href='/'; else document.getElementById('err').style.display='block';
}
</script></body></html>"""

REGISTER_HTML = """<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="theme-color" content="#1a5c8f"><link rel="manifest" href="/manifest.json"><link rel="apple-touch-icon" href="/icons/apple-touch-icon.png"><meta name="apple-mobile-web-app-capable" content="yes"><meta name="apple-mobile-web-app-status-bar-style" content="black-translucent"><title>水印相机 · 注册</title><style>
body{font-family:sans-serif;background:#eef1f5;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}
.card{background:#fff;border-radius:14px;padding:26px;width:300px;box-shadow:0 2px 10px rgba(0,0,0,.08)}
h2{text-align:center;color:#1a3d6d;margin:0 0 4px} .sub{font-size:12px;color:#999;text-align:center;margin-bottom:14px}
input{width:100%;padding:11px;border:1px solid #ddd;border-radius:8px;font-size:15px;margin:8px 0;box-sizing:border-box}
button{width:100%;padding:12px;border:none;border-radius:8px;background:#188038;color:#fff;font-size:16px;margin-top:4px}
.err{color:#d93025;font-size:13px;margin-top:8px;display:none;text-align:center}
.ok{color:#188038;font-size:13px;margin-top:8px;display:none;text-align:center}
.link{text-align:center;font-size:13px;margin-top:12px} a{color:#1a5c8f;text-decoration:none}
</style></head><body><a id="backHome" href="/" style="position:fixed;top:14px;left:14px;font-size:14px;color:#1a5c8f;text-decoration:none;background:#fff;padding:8px 12px;border-radius:8px;box-shadow:0 1px 6px rgba(0,0,0,.08);z-index:9">← 返回系统首页</a><script>(function(){var p=location.pathname;var a=document.getElementById('backHome');a.href=(p==='/wc'||p.indexOf('/wc/')===0)?'/':'https://bydawn.cpolar.cn/';})();</script>
<div class="card">
<h2>📷 水印相机</h2><div class="sub">注册账号后才能使用水印相机和查看相册</div>
<input id="name" placeholder="姓名（如：赵泽勇）">
<input id="user" placeholder="用户名（登录用，如：zhaozy）">
<input type="password" id="pw" placeholder="密码（至少4位）">
<button onclick="reg()">注 册</button>
<div class="err" id="err"></div><div class="ok" id="ok">✅ 注册成功，正在进入…</div>
<div class="link"><a href="/login">已有账号？去登录</a></div></div>
<script>
async function reg(){
 const name=document.getElementById('name').value.trim();
 const user=document.getElementById('user').value.trim();
 const pw=document.getElementById('pw').value;
 document.getElementById('err').style.display='none';
 if(!name||!user||pw.length<4){document.getElementById('err').textContent='请填姓名、用户名，密码至少4位';document.getElementById('err').style.display='block';return;}
 const r=await fetch('/api/register',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:name,user:user,pw:pw})});
 const j=await r.json();
 if(j.ok){document.getElementById('ok').style.display='block'; setTimeout(()=>location.href='/',600);}
 else {document.getElementById('err').textContent=j.err||'注册失败';document.getElementById('err').style.display='block';}
}
</script></body></html>"""

MENU_HTML = """<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="theme-color" content="#1a5c8f"><link rel="manifest" href="/manifest.json"><link rel="apple-touch-icon" href="/icons/apple-touch-icon.png"><meta name="apple-mobile-web-app-capable" content="yes"><meta name="apple-mobile-web-app-status-bar-style" content="black-translucent"><link rel="manifest" href="/manifest.json"><link rel="apple-touch-icon" href="/icons/apple-touch-icon.png"><meta name="apple-mobile-web-app-capable" content="yes"><meta name="apple-mobile-web-app-status-bar-style" content="black-translucent"><meta name="theme-color" content="#1a5c8f"><title>水印相机</title><style>
body{font-family:sans-serif;background:#eef1f5;margin:0;display:flex;align-items:center;justify-content:center;min-height:100vh}
.box{width:min(92vw,420px)}
.hd{text-align:center;margin-bottom:22px}
.hd h1{margin:0;font-size:22px;color:#1a3d6d}
.hd p{margin:6px 0 0;font-size:12px;color:#999}
.menu{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.item{background:#fff;border-radius:16px;padding:26px 10px;text-align:center;box-shadow:0 2px 10px rgba(0,0,0,.07);cursor:pointer;user-select:none}
.item .ic{font-size:40px}
.item .nm{font-size:17px;font-weight:700;color:#222;margin-top:10px}
.item .ds{font-size:11px;color:#999;margin-top:4px}
.item.cam{background:linear-gradient(180deg,#fff,#e8f1fb)}
.item.alb{background:linear-gradient(180deg,#fff,#eafaf1)}
.out{margin-top:22px;text-align:center;font-size:13px;color:#1a5c8f}
.user{font-size:12px;color:#888;text-align:center;margin-top:8px}
</style></head><body><div class="box">
<div class="hd"><h1>📷 水印相机</h1><p id="who"></p></div>
<div class="menu">
<div class="item cam" onclick="location.href='/cam?v=21'"><div class="ic">📸</div><div class="nm">拍照 / 录像</div><div class="ds">打开相机 · 水印实时显示</div></div>
<div class="item alb" onclick="location.href='/album'"><div class="ic">🖼️</div><div class="nm">相册</div><div class="ds">查看 NAS 水印照片/录像</div></div>
</div>
<div class="out"><a href="https://bydawn.cpolar.cn/portal" target="_blank">→ 施工签证单系统</a></div>
<div class="out"><a href="javascript:logout()">退出登录</a></div>
<div class="user" id="us"></div></div>
<script>
fetch('/api/me').then(r=>r.json()).then(j=>{ if(j.ok){ document.getElementById('us').textContent='当前用户：'+j.name+'（'+j.user+'）'; } });
function logout(){ document.cookie='wc_auth=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/'; location.href='/login'; }
</script></body></html>"""

ALBUM_HTML_TMPL = r"""<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><link rel="manifest" href="/manifest.json"><link rel="apple-touch-icon" href="/icons/apple-touch-icon.png"><meta name="apple-mobile-web-app-capable" content="yes"><meta name="apple-mobile-web-app-status-bar-style" content="black-translucent"><meta name="theme-color" content="#1a5c8f"><title>水印相册</title><style>
body{font-family:sans-serif;background:#eef1f5;margin:0;padding:12px}
.top{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px}
h2{margin:0;font-size:17px;color:#1a3d6d}
a{color:#1a5c8f;text-decoration:none;font-size:13px}
.day{background:#fff;border-radius:12px;padding:12px;margin-bottom:12px;box-shadow:0 1px 5px rgba(0,0,0,.05)}
.day h3{font-size:14px;color:#333;margin:0 0 10px}
.g{display:grid;grid-template-columns:repeat(2,1fr);gap:10px}
.ph{border:1px solid #e3e8ee;border-radius:10px;overflow:hidden;background:#fff;position:relative;cursor:pointer}
.ph img{width:100%;height:130px;object-fit:cover;display:block;background:#f4f6f9}
.ph .meta{font-size:11px;color:#666;padding:6px 8px;line-height:1.6}
.ph .play{position:absolute;top:0;left:0;right:0;height:130px;display:flex;align-items:center;justify-content:center;background:#0b0f14;color:#fff;font-size:26px}
.ph .play span{font-size:11px;position:absolute;bottom:8px;left:0;right:0;text-align:center;opacity:.85}
.empty{color:#aaa;text-align:center;padding:30px;font-size:14px}
/* ============ 全屏查看器（v46 新增） ============ */
.vw{position:fixed;inset:0;background:#000;display:none;z-index:200}
.vw.on{display:block}
.vstage{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;overflow:hidden}
.vstage img,.vstage video{max-width:100%;max-height:100%;object-fit:contain;display:block}
.vload{position:absolute;top:50%;left:0;right:0;text-align:center;color:#9fb4c8;font-size:13px;transform:translateY(-50%);pointer-events:none}
.vwtop{position:absolute;top:0;left:0;right:0;z-index:3;display:flex;align-items:center;gap:10px;padding:10px 12px;padding-top:calc(10px + env(safe-area-inset-top));background:linear-gradient(rgba(0,0,0,.72),rgba(0,0,0,0))}
.vbtn{background:rgba(0,0,0,.42);color:#fff;border:1px solid rgba(255,255,255,.32);border-radius:10px;padding:8px 14px;font-size:14px;font-family:inherit}
.vbtn:active{background:rgba(255,255,255,.34)}
.vwttl{flex:1;color:#e8eef5;font-size:12.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.vwidx{color:#cfe0f0;font-size:13px}
.vnav{position:absolute;top:50%;transform:translateY(-50%);z-index:3;width:48px;height:104px;border:1px solid rgba(255,255,255,.3);border-radius:12px;background:rgba(0,0,0,.46);color:#fff;font-size:30px;line-height:1;display:flex;align-items:center;justify-content:center;box-shadow:0 2px 10px rgba(0,0,0,.45)}
.vnav:active{background:rgba(255,255,255,.34)}
.vnav.l{left:8px}.vnav.r{right:8px}
.vcap{position:absolute;bottom:0;left:0;right:0;z-index:3;padding:10px 12px calc(10px + env(safe-area-inset-bottom));background:linear-gradient(rgba(0,0,0,0),rgba(0,0,0,.78));color:#dfe8f2;font-size:11.5px;line-height:1.65}
.vdel{background:rgba(168,32,32,.62);border-color:rgba(255,255,255,.34);margin-left:2px}
.vdel:active{background:rgba(190,34,34,.9)}
.cf{position:fixed;inset:0;background:rgba(0,0,0,.62);display:none;align-items:center;justify-content:center;z-index:300;padding:24px}
.cf.on{display:flex}
.cfbox{background:#fff;border-radius:16px;padding:20px 20px 16px;max-width:320px;width:100%;box-shadow:0 12px 40px rgba(0,0,0,.4)}
.cfttl{font-size:16px;font-weight:700;color:#1b2229;text-align:center}
.cfsub{font-size:12.5px;color:#5a6874;line-height:1.7;margin:8px 0 4px;text-align:center}
.cfwho{font-size:12px;color:#1a5c8f;background:#f2f7fb;border-radius:8px;padding:8px 10px;margin:10px 0 4px;line-height:1.6;word-break:break-all}
.cfbtns{display:flex;gap:10px;margin-top:14px}
.cfbtns .vbtn{flex:1;background:#eef1f5;color:#39434d;border:1px solid #d8dee6;padding:11px 0;font-size:15px}
.cfbtns .vbtn:active{background:#e2e7ee}
.cfbtns .vdel2{background:#c62828;color:#fff;border:0}
.cfbtns .vdel2:active{background:#a51e1e}
</style></head><body>
<div class="top"><h2>📷 水印相册</h2><div><a href="/report">🧩 拼图汇报</a>　<a href="/">← 拍照</a>　<a href="javascript:logout()">退出</a></div></div>
<div class="card" style="background:#fff;border-radius:12px;padding:10px;margin-bottom:12px;box-shadow:0 1px 5px rgba(0,0,0,.05)">
  <input id="sq" placeholder="🔍 搜水印文字：如 施工区域 / 地磅 / 张三" style="width:100%;padding:11px;border:1px solid #e3e8ee;border-radius:10px;font-size:15px;box-sizing:border-box"
    oninput="deb()" onkeydown="if(event.key=='Enter')load()">
  <div style="font-size:12px;color:#98a2b3;margin-top:6px" id="sres"></div>
</div>
<div id="box"><div class="empty">加载中…</div></div>

<div id="vw" class="vw">
  <div class="vstage" id="vstage"></div>
  <div class="vwtop">
    <button class="vbtn" onclick="closeVw()">← 返回</button>
    <div class="vwttl" id="vwttl"></div>
    <div class="vwidx" id="vwidx"></div>
    <button class="vbtn vdel" onclick="askDel()">删除</button>
  </div>
  <button class="vnav l" onclick="stepV(-1)" aria-label="上一张">‹</button>
  <button class="vnav r" onclick="stepV(1)" aria-label="下一张">›</button>
  <div class="vcap" id="vcap"></div>
</div>

<div id="cf" class="cf">
  <div class="cfbox">
    <div class="cfttl">确定删除这张？</div>
    <div class="cfwho" id="cfwho"></div>
    <div class="cfsub">删除后照片会移入 NAS 上的「水印相机回收站」，可以找回。</div>
    <div class="cfbtns">
      <button class="vbtn" onclick="closeCf()">取消</button>
      <button class="vbtn vdel2" onclick="doDel()">删除</button>
    </div>
  </div>
</div>

<script>
let tmr=null, FLAT=[], VI=0, vwOpen=false;
function deb(){ clearTimeout(tmr); tmr=setTimeout(load,350); }
function thumbURL(f){ return '/photo/'+f.o+'/'+f.d+'/'+f.n+'?w=360'; }
function origURL(f){ return '/photo/'+f.o+'/'+f.d+'/'+f.n; }
async function load(){
 const kw=document.getElementById('sq').value.trim();
 const r=await fetch('/api/photos'+(kw?'?q='+encodeURIComponent(kw):'')); const j=await r.json();
 const box=document.getElementById('box'); box.innerHTML=''; FLAT=[];
 document.getElementById('sres').textContent = kw ? ('匹配 '+((j.n)||0)+' 张') : '共 '+((j.n)||0)+' 张，输入关键词可按水印文字筛选';
 if(!j.days||!j.days.length){box.innerHTML='<div class="empty">'+(kw?'没搜到相关照片':'还没有照片，先去拍一张吧')+'</div>';return;}
 j.days.forEach(d=>{
   const dv=document.createElement('div'); dv.className='day';
   dv.dataset.day=d.day;
   dv.innerHTML='<h3>📅 '+d.day+'（'+d.files.length+'张）</h3><div class="g"></div>';
   const g=dv.querySelector('.g');
   d.files.forEach(f=>{
     const m=f.meta||{};
     const isVid=/\\.(mp4|webm)$/i.test(f.name);
     const rec={o:f.owner,d:d.day,n:f.name,m:m,isVid:isVid,mine:!!f.mine};
     rec.key=f.owner+'|'+d.day+'|'+f.name;
     const idx=FLAT.length; FLAT.push(rec);
     const el=document.createElement('div'); el.className='ph';
     el.dataset.key=rec.key;
     const th=isVid
       ? '<div class="play">▶<span>录像</span></div>'
       : '<img src="'+thumbURL(rec)+'" loading="lazy" decoding="async" alt="">';
     el.innerHTML=th+'<div class="meta">👤 '+(f.owner||'')+' · '+((m['类型']||'')+(m['施工区域']?' '+m['施工区域']:'')+(m['施工内容']?' '+m['施工内容']:'')).trim()+'<br>'+((m['拍摄时间']||'')+(m['地点']?' '+m['地点']:'')).trim()+'</div>';
     el.onclick=(function(kk){ return function(){ openVByKey(kk); }; })(rec.key);
     g.appendChild(el);
   });
   box.appendChild(dv);
 });
}
/* ---------- 全屏查看器 ---------- */
function openV(i){
 if(!FLAT.length) return;
 VI=Math.max(0,Math.min(FLAT.length-1,i));
 vwOpen=true;
 document.getElementById('vw').classList.add('on');
 document.body.style.overflow='hidden';
 showV();
}
function closeVw(){
 vwOpen=false;
 document.getElementById('vw').classList.remove('on');
 document.getElementById('vstage').innerHTML='';
 document.body.style.overflow='';
}
function showV(){
 const f=FLAT[VI]; if(!f) return;
 const st=document.getElementById('vstage');
 const m=f.m||{};
 st.innerHTML='<div class="vload" id="vload">加载中…</div>';
 const node=document.createElement(f.isVid?'video':'img');
 node.src=origURL(f);
 if(f.isVid){ node.controls=true; node.playsInline=true; node.autoplay=true; node.muted=false; node.style.background='#000'; }
 else { node.alt=''; node.onload=function(){ var l=document.getElementById('vload'); if(l) l.remove(); }; }
 node.onerror=function(){ var l=document.getElementById('vload'); if(l) l.textContent='这张加载失败'; };
 st.appendChild(node);
 document.getElementById('vwidx').textContent=(VI+1)+' / '+FLAT.length;
 var _db=document.querySelector('.vdel'); if(_db){ _db.style.display=f.mine ? '' : 'none'; }
 document.getElementById('vwttl').textContent=m['拍摄时间']||f.d;
 document.getElementById('vcap').innerHTML='👤 '+(f.o||'')+'　📅 '+f.d+'<br>'+((m['类型']||'')+(m['施工区域']?' · '+m['施工区域']:'')+(m['施工内容']?' · '+m['施工内容']:'')).trim()+((m['地点']?'<br>📍 '+m['地点']:''));
 /* 预加载相邻，翻页秒出 */
 [VI-1,VI+1].forEach(function(k){
   if(k<0||k>=FLAT.length) return;
   var nf=FLAT[k]; if(nf.isVid) return;
   var im=new Image(); im.src=origURL(nf);
 });
}
function stepV(d){
 if(!vwOpen) return;
 if(FLAT.length<2) return;
 VI=(VI+d+FLAT.length)%FLAT.length;
 showV();
}
function openVByKey(k){
 for(var i=0;i<FLAT.length;i++){ if(FLAT[i].key===k){ openV(i); return; } }
}
/* ---------- 删除（移到回收站，可找回） ---------- */
var delIdx=-1;
function askDel(){
 if(!vwOpen) return;
 var f=FLAT[VI]; if(!f) return;
 if(!f.mine){ alert('只能删除自己拍摄的照片'); return; }
 delIdx=VI;
 var m=f.m||{};
 document.getElementById('cfwho').innerHTML='👤 '+(f.o||'')+'　📅 '+f.d
   +((m['拍摄时间'])?'<br>🕒 '+m['拍摄时间']:'')
   +((m['施工区域'])?'<br>📍 '+m['施工区域']:'');
 document.getElementById('cf').classList.add('on');
}
function closeCf(){ document.getElementById('cf').classList.remove('on'); delIdx=-1; }
async function doDel(){
 var i=(delIdx>=0)?delIdx:VI;
 var f=FLAT[i];
 closeCf();
 if(!f) return;
 try{
   var r=await fetch('/api/delete',{method:'POST',headers:{'Content-Type':'application/json'},
     body:JSON.stringify({owner:f.o,day:f.d,name:f.n})});
   var j=await r.json();
   if(!j.ok){ alert('删除失败：'+((j&&j.err)||'未知错误')); return; }
   var card=document.querySelector('.ph[data-key="'+f.key+'"]');
   if(card){
     var dayEl=card.closest('.day');
     card.remove();
     if(dayEl){
       var left=dayEl.querySelectorAll('.ph').length;
       var h=dayEl.querySelector('h3');
       if(h) h.textContent='📅 '+(dayEl.dataset.day||'')+'（'+left+'张）';
       if(!left) dayEl.remove();
     }
   }
   FLAT.splice(i,1);
   var sr=document.getElementById('sres');
   if(sr) sr.textContent='共 '+FLAT.length+' 张（已删除 1 张，可在「水印相机回收站」找回）';
   if(!FLAT.length){ closeVw(); var bx=document.getElementById('box'); if(bx) bx.innerHTML='<div class="empty">还没有照片，先去拍一张吧</div>'; return; }
   VI=Math.min(i,FLAT.length-1);
   showV();
 }catch(e){ alert('删除失败：'+e.message); }
}
document.addEventListener('keydown',function(e){
 if(!vwOpen) return;
 if(e.key==='ArrowLeft') stepV(-1);
 else if(e.key==='ArrowRight') stepV(1);
 else if(e.key==='Escape') closeVw();
});
/* 左右滑动切换（查看器内） */
let tx=0,ty=0;
document.getElementById('vw').addEventListener('touchstart',function(e){
 if(e.touches.length!==1) return;
 tx=e.touches[0].clientX; ty=e.touches[0].clientY;
},{passive:true});
document.getElementById('vw').addEventListener('touchend',function(e){
 if(!vwOpen||e.changedTouches.length!==1) return;
 const dx=e.changedTouches[0].clientX-tx, dy=e.changedTouches[0].clientY-ty;
 if(Math.abs(dx)>56 && Math.abs(dx)>Math.abs(dy)*1.4) stepV(dx<0?1:-1);
},{passive:true});
function logout(){ document.cookie='wc_auth=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/'; location.href='/login'; }
load();
</script></body></html>"""

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _send(self, code, body, ctype="application/json; charset=utf-8", cookie=None, cache=None):
        b = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", cache or "no-store")
        # 相机/麦克风/定位：明确允许同源使用（配合浏览器"授权一次"记忆）
        self.send_header("Permissions-Policy", "camera=(self), microphone=(self), geolocation=(self)")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        try: self.wfile.write(b)
        except Exception: pass

    def current_user(self):
        m = re.search(COOKIE + r"=([^;]+)", self.headers.get("Cookie", ""))
        if m:
            return check_token(m.group(1).strip())
        return None

    def do_GET(self):
        u = urlparse(self.path)
        p = u.path
        q = parse_qs(u.query)
        if p == "/login":
            self._send(200, LOGIN_HTML, "text/html; charset=utf-8"); return
        if p == "/register":
            self._send(200, REGISTER_HTML, "text/html; charset=utf-8"); return
        if p == "/manifest.json":
            try:
                data = open(os.path.join(BASE, "manifest.json"), "rb").read()
                self._send(200, data.decode("utf-8", "replace"), "application/manifest+json; charset=utf-8")
            except Exception:
                self._send(404, json.dumps({"ok": False, "err": "404"}))
            return
        if p.startswith("/icons/"):
            fn = os.path.basename(p)
            fp = os.path.join(BASE, "icons", fn)
            if os.path.isfile(fp) and fn.endswith(".png"):
                self._send(200, open(fp, "rb").read(), "image/png")
            else:
                self._send(404, json.dumps({"ok": False, "err": "404"}))
            return
        user = self.current_user()
        if not user:
            self._send(200, '<meta http-equiv="refresh" content="0;url=/login"><h3>请先登录</h3><a href="/login">去登录</a>', "text/html; charset=utf-8"); return
        if p == "/":
            self._send(200, MENU_HTML, "text/html; charset=utf-8")
        elif p == "/cam":
            idx = open(os.path.join(BASE, "index.html"), encoding="utf-8").read()
            self._send(200, idx, "text/html; charset=utf-8")
        elif p == "/album":
            self._send(200, ALBUM_HTML_TMPL, "text/html; charset=utf-8")
        elif p == "/report":
            self._send(200, REPORT_HTML, "text/html; charset=utf-8")
        elif p == "/api/me":
            us = load_users()
            nm = (us.get(user) or {}).get("name", user)
            self._send(200, json.dumps({"ok": True, "user": user, "name": nm}, ensure_ascii=False))
        elif p == "/api/photos":
            kw = (q.get("q", [""])[0] or "").strip()
            day = (q.get("day", [""])[0] or "").strip()
            me_name = (load_users().get(user) or {}).get("name") or user
            days = list_photos(day=day or None, q=kw or None, me_name=me_name)
            n = sum(len(x["files"]) for x in days)
            self._send(200, json.dumps({"ok": True, "days": days, "n": n, "q": kw}, ensure_ascii=False))
        elif p == "/api/prefs":
            allp = load_prefs()
            self._send(200, json.dumps({"ok": True, "prefs": (allp.get(user) or {})}, ensure_ascii=False))
        elif p == "/api/weather":
            lat = q.get("lat", [""])[0]; lng = q.get("lng", [""])[0]
            self._send(200, json.dumps(get_weather(lat or None, lng or None), ensure_ascii=False))
        elif p == "/api/regeo":
            lat = q.get("lat", [""])[0]; lng = q.get("lng", [""])[0]
            self._send(200, json.dumps(regeo(lat, lng), ensure_ascii=False))
        elif p.startswith("/photo/"):
            rel = unquote(p[len("/photo/"):])
            if not re.match(r"^[^/]+/\d{4}-\d{2}-\d{2}/[\w.\-]+\.(jpg|jpeg|png|mp4|webm)$", rel, re.I):
                self._send(404, json.dumps({"ok": False, "err": "404"})); return
            fp = os.path.join(SAVE_ROOT, rel)
            if not os.path.isfile(fp):
                self._send(404, json.dumps({"ok": False, "err": "404"})); return
            ext = rel.lower().rsplit(".", 1)[-1]
            ct = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png",
                  "mp4": "video/mp4", "webm": "video/webm"}.get(ext, "application/octet-stream")
            # v46：缩略图 /photo/<owner>/<day>/<name>?w=360 —— PIL 缩放 + 磁盘缓存（放 BASE/__thumbs，不污染相册目录）
            wq = (q.get("w", [""])[0] or "").strip()
            if wq.isdigit() and ext in ("jpg", "jpeg", "png"):
                tw = max(80, min(1600, int(wq)))
                tkey = os.path.join(BASE, "__thumbs", str(tw), rel)
                try:
                    if not os.path.isfile(tkey):
                        os.makedirs(os.path.dirname(tkey), exist_ok=True)
                        im = Image.open(fp)
                        if im.mode not in ("RGB", "L"):
                            im = im.convert("RGB")
                        im.thumbnail((tw, tw * 4), Image.LANCZOS)
                        tmp = tkey + "." + str(os.getpid()) + ".tmp"
                        im.save(tmp, "JPEG", quality=82, optimize=True)
                        os.replace(tmp, tkey)
                    self._send(200, open(tkey, "rb").read(), "image/jpeg", cache="public, max-age=604800")
                    return
                except Exception:
                    pass  # 缩略图失败就回退发原图
            data = open(fp, "rb").read()
            self._send(200, data, ct, cache="public, max-age=86400")
        else:
            self._send(404, json.dumps({"ok": False, "err": "404"}))

    def do_POST(self):
        u = urlparse(self.path)
        ln = int(self.headers.get("Content-Length", 0))
        ctype = (self.headers.get("Content-Type") or "").lower()
        # 二进制直传（2026-09-12 新增）：省掉 base64 的 33% 体积膨胀 + 前端二次编码
        if u.path in ("/api/photo", "/api/video") and "json" not in ctype and ln > 0:
            raw = self.rfile.read(ln)
            q = parse_qs(u.query)
            ext = (self.headers.get("X-File-Ext") or "").strip().lower()
            is_vid = (u.path == "/api/video")
            if is_vid:
                if ext not in ("mp4", "webm"):
                    ext = "webm"
                d = {"kind": "video"}
            else:
                if ext not in ("jpg", "jpeg", "png"):
                    ext = "jpg"
                d = {}
            d.update({k: (v[0] if v else "") for k, v in q.items()})
            d["_raw"] = raw
            d["_ext"] = ext
            user = self.current_user()
            if not user:
                self._send(200, json.dumps({"ok": False, "err": "未登录"}, ensure_ascii=False)); return
            self.handle_photo(d, user)
            return
        try:
            d = json.loads(self.rfile.read(ln).decode("utf-8", "replace"))
        except Exception:
            d = {}
        if u.path == "/api/register":
            name = (d.get("name") or "").strip()
            user = (d.get("user") or "").strip()
            pw = d.get("pw") or ""
            if not name or not re.match(r"^[\w\u4e00-\u9fa5\-]{1,32}$", user) or len(pw) < 4:
                self._send(200, json.dumps({"ok": False, "err": "请正确填写：姓名、用户名(字母数字)，密码≥4位"}, ensure_ascii=False)); return
            with _lock:
                us = load_users()
                if user in us:
                    self._send(200, json.dumps({"ok": False, "err": "用户名已存在"}, ensure_ascii=False)); return
                us[user] = {"name": name, "pw": hash_pw(pw), "created": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
                save_users(us)
            ck = (COOKIE + "=" + make_token(user) + "; Path=/; Max-Age=" + str(COOKIE_MAXAGE)
                  + "; SameSite=Lax")   # 注册成功自动登录（长期有效）
            self._send(200, json.dumps({"ok": True, "user": user}, ensure_ascii=False), cookie=ck); return
        if u.path == "/api/prefs":
            who2 = self.current_user()
            if not who2:
                self._send(200, json.dumps({"ok": False, "err": "未登录"}, ensure_ascii=False)); return
            obj = d if isinstance(d, dict) else {}
            allp = load_prefs()
            obj["ts"] = int(obj.get("ts") or (time.time() * 1000))
            allp[who2] = obj
            okp = save_prefs(allp)
            self._send(200, json.dumps({"ok": okp, "n": len(obj)}, ensure_ascii=False)); return
        if u.path == "/api/login":
            user = (d.get("user") or "").strip()
            pw = d.get("pw") or ""
            us = load_users()
            rec = us.get(user)
            if rec and check_pw(pw, rec.get("pw", "")):
                ck = (COOKIE + "=" + make_token(user) + "; Path=/; Max-Age=" + str(COOKIE_MAXAGE)
                      + "; SameSite=Lax")
                self._send(200, json.dumps({"ok": True, "user": user}, ensure_ascii=False), cookie=ck)
            else:
                self._send(200, json.dumps({"ok": False, "err": "用户名或密码错误"}, ensure_ascii=False))
            return
        user = self.current_user()
        if not user:
            self._send(200, json.dumps({"ok": False, "err": "未登录"}, ensure_ascii=False)); return
        if u.path == "/api/delete":
            # v46：删除单张 = 移到 TRASH_ROOT（可找回）。路径白名单 + realpath 圈定 + 只能删自己拍的
            owner = (d.get("owner") or "").strip()
            day = (d.get("day") or "").strip()
            name = (d.get("name") or "").strip()
            if (not re.match(r"^[\w\u4e00-\u9fa5\-]{1,64}$", owner)
                    or not re.match(r"^\d{4}-\d{2}-\d{2}$", day)
                    or not re.match(r"^[\w.\-]+\.(jpg|jpeg|png|mp4|webm)$", name, re.I)):
                self._send(200, json.dumps({"ok": False, "err": "参数不合法"}, ensure_ascii=False)); return
            src = os.path.join(SAVE_ROOT, owner, day, name)
            broot = os.path.realpath(SAVE_ROOT)
            if (not os.path.realpath(src).startswith(broot + os.sep)) or (not os.path.isfile(src)):
                self._send(200, json.dumps({"ok": False, "err": "文件不存在"}, ensure_ascii=False)); return
            _txt = os.path.splitext(src)[0] + ".txt"
            _rec = ""
            if os.path.isfile(_txt):
                try:
                    for _ln in open(_txt, encoding="utf-8"):
                        if _ln.startswith("记录人："):
                            _rec = _ln.split("：", 1)[1].strip(); break
                except Exception:
                    _rec = ""
            if not _rec:
                _rec = owner
            _me = (load_users().get(user) or {}).get("name") or user
            if _rec != _me:
                self._send(200, json.dumps({"ok": False, "err": "只能删除自己拍摄的照片（记录人：" + str(_rec) + "）"}, ensure_ascii=False)); return
            dst_dir = os.path.join(TRASH_ROOT, owner, day)
            try:
                os.makedirs(dst_dir, exist_ok=True)
                dst = os.path.join(dst_dir, name)
                if os.path.exists(dst):
                    dst = os.path.join(dst_dir, str(int(time.time())) + "_" + name)   # 重名不覆盖
                os.replace(src, dst)
                try:
                    os.utime(dst, None)          # v46d：mtime = 删除时刻，30 天倒计时从这里算
                except Exception:
                    pass
                try:                             # 回收站目录与照片目录同属主，方便用户自己管
                    _st = os.stat(SAVE_ROOT)
                    for _p in (TRASH_ROOT, os.path.join(TRASH_ROOT, owner), dst_dir):
                        try:
                            os.chown(_p, _st.st_uid, _st.st_gid)
                        except Exception:
                            pass
                except Exception:
                    pass
                if os.path.isfile(_txt):
                    os.replace(_txt, os.path.join(dst_dir, os.path.splitext(os.path.basename(dst))[0] + ".txt"))
                self._send(200, json.dumps({"ok": True, "by": user,
                                            "file": os.path.relpath(dst, os.path.dirname(TRASH_ROOT))}, ensure_ascii=False))
            except Exception as e:
                self._send(200, json.dumps({"ok": False, "err": "删除失败：" + str(e)}, ensure_ascii=False))
            return
        if u.path == "/api/photo":
            self.handle_photo(d, user)
            return
        if u.path == "/api/report":
            body = d if isinstance(d, dict) else {}      # JSON POST 时 d 已是解析好的对象
            items = body.get("files") or []
            if not items:
                self._send(400, json.dumps({"ok": False, "err": "没有选择照片"}, ensure_ascii=False)); return
            try:
                rel = build_report(items[:9], body.get("layout") or "2",
                                   (body.get("title") or "").strip(), (body.get("who") or "").strip(), user)
                self._send(200, json.dumps({"ok": True, "rel": rel}, ensure_ascii=False))
            except Exception as e:
                self._send(500, json.dumps({"ok": False, "err": str(e)}, ensure_ascii=False))
            return
        if u.path == "/api/video":
            self.handle_photo(dict(d, kind="video"), user)
            return
        self._send(404, json.dumps({"ok": False, "err": "404"}))

    def handle_photo(self, d, user):
        img = d.get("img") or ""
        is_video = d.get("kind") == "video"
        raw = d.get("_raw")
        if raw is not None:
            # 二进制直传：扩展名从 X-File-Ext 来，无需 base64 解码
            ext = (d.get("_ext") or ("mp4" if is_video else "jpg")).lower()
            allowed = ("mp4", "webm") if is_video else ("jpg", "jpeg", "png")
            if ext not in allowed:
                ext = "mp4" if is_video else "jpg"
            if ext == "jpeg":
                ext = "jpg"
            maxb = (120 if is_video else 25) * 1024 * 1024
            if len(raw) > maxb:
                self._send(413, json.dumps({"ok": False, "err": "文件超过" + str(maxb // 1048576) + "MB"}, ensure_ascii=False)); return
        else:
            if is_video:
                m = re.match(r"data:(video/(webm|mp4));base64,(.*)", img, re.S)
                ext = "mp4" if m and m.group(2) == "mp4" else "webm"
                maxb = 120 * 1024 * 1024
            else:
                m = re.match(r"data:image/(png|jpeg|jpg);base64,(.*)", img, re.S)
                ext = "jpg" if m and m.group(1) in ("jpeg", "jpg") else "png"
                maxb = 25 * 1024 * 1024
            if not m:
                self._send(400, json.dumps({"ok": False, "err": "数据缺失或格式错误"}, ensure_ascii=False)); return
            try:
                raw = base64.b64decode(m.group(3) if is_video else m.group(2))
            except Exception:
                self._send(400, json.dumps({"ok": False, "err": "解码失败"}, ensure_ascii=False)); return
            if len(raw) > maxb:
                self._send(413, json.dumps({"ok": False, "err": "文件超过" + str(maxb // 1048576) + "MB"}, ensure_ascii=False)); return
        now = datetime.datetime.now()
        us = load_users()
        who = (us.get(user) or {}).get("name", user) or "未命名"
        safe_name = re.sub(r"[\\/:*?\"<>|\s]", "_", who).strip() or "未命名"
        # v40：按「项目名称」分文件夹（项目变了就自动新建一个）；没传项目名 → 回退按记录人
        _pj = re.sub(r'[\\/:*?"<>|\s]+', "_", (d.get("project") or "").strip()).strip("_")[:60]
        folder_key = _pj or safe_name
        day = now.strftime("%Y-%m-%d")
        folder = os.path.join(SAVE_ROOT, folder_key, day)
        for sub in (os.path.join(SAVE_ROOT, folder_key), folder):
            try:
                os.makedirs(sub, exist_ok=True)
                os.chmod(sub, 0o777)
            except Exception:
                pass
        ts = now.strftime("%Y%m%d_%H%M%S")
        name = "%s_%s.%s" % (ts, "".join(random.choices(string.ascii_lowercase + string.digits, k=3)), ext)
        path = os.path.join(folder, name)
        with open(path, "wb") as f:
            f.write(raw)
        meta = {
            "类型": "录像" if is_video else "照片",
            "项目名称": d.get("project") or "",
            "记录人": who,
            "拍摄时间": d.get("time") or now.strftime("%Y-%m-%d %H:%M"),
            "施工区域": d.get("area") or "",
            "施工内容": d.get("content") or "",
            "天气": d.get("weather") or "",
            "地点": d.get("place") or "",
            "坐标": d.get("coords") or "",
        }
        with open(os.path.splitext(path)[0] + ".txt", "w", encoding="utf-8") as f:
            f.write("水印相机记录\n")
            for k, v in meta.items():
                f.write("%s：%s\n" % (k, v))
        rel = "DawnAI输出/水印相机照片/%s/%s/%s" % (folder_key, day, name)
        self._send(200, json.dumps({"ok": True, "file": rel, "size": len(raw)}, ensure_ascii=False))

threading.Thread(target=_trash_worker, daemon=True).start()   # v46d 回收站 30 天清理

if __name__ == "__main__":
    os.makedirs(SAVE_ROOT, exist_ok=True)
    print("水印相机 v2 http://0.0.0.0:8335/")
    ThreadingHTTPServer(("0.0.0.0", 8335), H).serve_forever()
