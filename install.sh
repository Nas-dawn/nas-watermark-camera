#!/bin/bash
# ============================================================
#  水印相机 · 一键安装脚本
#  用法（需 root/sudo）：
#     sudo bash install.sh                                  # 全部用默认值
#     sudo bash install.sh --app /vol1/mydisk/wmcam \
#          --photos /vol1/mydisk/水印相机照片 --port 8335
#  参数：
#     --app    程序安装目录（默认 /opt/wmcam）
#     --photos 照片保存目录（默认 <app>/photos）
#     --port   服务端口（默认 8335）
#     --python 指定 python 解释器（默认自动探测 python3）
#     --uninstall 卸载（停服务、删 unit；程序目录与照片目录保留）
# ============================================================
set -e

APP_DIR="/opt/wmcam"
PHOTO_DIR=""
PORT="8335"
PY=""
DO_UNINSTALL=0
NO_SERVICE=0

while [ $# -gt 0 ]; do
  case "$1" in
    --app) APP_DIR="$2"; shift 2;;
    --photos) PHOTO_DIR="$2"; shift 2;;
    --port) PORT="$2"; shift 2;;
    --python) PY="$2"; shift 2;;
    --no-service) NO_SERVICE=1; shift;;
    --uninstall) DO_UNINSTALL=1; shift;;
    *) echo "未知参数: $1"; exit 1;;
  esac
done

SERVICE="wmcam-$PORT"
UNIT="/etc/systemd/system/$SERVICE.service"
HERE="$(cd "$(dirname "$0")" && pwd)"
[ -z "$PHOTO_DIR" ] && PHOTO_DIR="$APP_DIR/photos"

if [ "$DO_UNINSTALL" = "1" ]; then
  echo "== 卸载 =="
  systemctl stop "$SERVICE" 2>/dev/null || true
  systemctl disable "$SERVICE" 2>/dev/null || true
  rm -f "$UNIT"; systemctl daemon-reload
  echo "已卸载服务 $SERVICE。程序目录 $APP_DIR 与照片目录 $PHOTO_DIR 已保留，如需删除请自行处理。"
  exit 0
fi

echo "================ 水印相机 安装 ================"
echo "  程序目录: $APP_DIR"
echo "  照片目录: $PHOTO_DIR"
echo "  端口:     $PORT"
echo "==================================================="

# 1) python 解释器
if [ -z "$PY" ]; then
  for c in python3 /usr/bin/python3; do command -v "$c" >/dev/null 2>&1 && { PY="$(command -v "$c")"; break; }; done
fi
[ -z "$PY" ] && { echo "✗ 没找到 python3，请先安装（apt install python3）"; exit 1; }
echo "· python: $PY ($("$PY" -V 2>&1))"

# 2) Pillow 依赖：先尝试自动装，装不上再给手动指引
if ! "$PY" -c "import PIL" >/dev/null 2>&1; then
  echo "· 未检测到 Pillow，尝试自动安装…"
  if command -v apt-get >/dev/null 2>&1; then
    apt-get install -y python3-pil >/dev/null 2>&1 || true
  fi
  if ! "$PY" -c "import PIL" >/dev/null 2>&1; then
    "$PY" -m pip install --quiet Pillow >/dev/null 2>&1 || true
  fi
fi
if ! "$PY" -c "import PIL" >/dev/null 2>&1; then
  echo "✗ 仍缺少 Pillow（图像处理库）。请任选一种装上后重跑："
  echo "      apt install -y python3-pil                       # Debian/Ubuntu/飞牛 fnOS"
  echo "      $PY -m pip install Pillow                        # 或用 pip"
  echo "  或者用已装好 Pillow 的解释器安装：  sudo bash install.sh --python /路径/python3"
  exit 1
fi
echo "· Pillow: $("$PY" -c 'import PIL;print(PIL.__version__)')"

# 3) 复制程序文件
mkdir -p "$APP_DIR"
cp -f "$HERE/app/server.py"    "$APP_DIR/server.py"
cp -f "$HERE/app/index.html"   "$APP_DIR/index.html"
cp -f "$HERE/app/manifest.json" "$APP_DIR/manifest.json"
mkdir -p "$APP_DIR/fonts" "$APP_DIR/icons"
cp -f "$HERE"/app/fonts/*  "$APP_DIR/fonts/" 2>/dev/null || true
cp -f "$HERE"/app/icons/*  "$APP_DIR/icons/" 2>/dev/null || true
mkdir -p "$PHOTO_DIR"
echo "· 程序文件已复制"

# 4) 写入照片目录与端口（server.py 里两处常量）
"$PY" - "$APP_DIR/server.py" "$PHOTO_DIR" "$PORT" <<'PYEOF'
import re, sys
p, photos, port = sys.argv[1], sys.argv[2], sys.argv[3]
s = open(p, encoding="utf-8").read()
s = re.sub(r'^SAVE_ROOT = .*$', 'SAVE_ROOT = "%s"' % photos, s, count=1, flags=re.M)
s = re.sub(r'ThreadingHTTPServer\(\("0\.0\.0\.0", \d+\)', 'ThreadingHTTPServer(("0.0.0.0", %s)' % port, s, count=1)
open(p, "w", encoding="utf-8").write(s)
print("· 已写入：SAVE_ROOT =", photos, "/ 端口 =", port)
PYEOF

if [ "$NO_SERVICE" = "1" ]; then
  echo "· 跳过 systemd（--no-service）。手动启动："
  echo "      cd $APP_DIR && $PY server.py &"
  echo "  安装路径：$APP_DIR ｜ 照片：$PHOTO_DIR ｜ 端口：$PORT"
  exit 0
fi

# 5) systemd 服务
cat > "$UNIT" <<EOF
[Unit]
Description=Guoding Watermark Camera ($PORT)
After=network.target

[Service]
WorkingDirectory=$APP_DIR
ExecStart=$PY server.py
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable "$SERVICE" >/dev/null 2>&1 || true
systemctl restart "$SERVICE"
sleep 2

echo "· 服务状态: $(systemctl is-active "$SERVICE")"
if command -v curl >/dev/null 2>&1; then
  code=$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/login" || true)
  echo "· 自检 http://127.0.0.1:$PORT/login -> HTTP $code"
fi

IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo
echo "================ 安装完成 ================"
echo "  打开： http://${IP:-<本机IP>}:$PORT/"
echo "  首次进去点「注册」建第一个账号；以后用同一账号登录即可。"
echo "  照片保存在： $PHOTO_DIR/<记录人|项目>/<日期>/"
echo "  删除的照片进： $(dirname "$PHOTO_DIR")/水印相机回收站/（保留 30 天自动清理）"
echo "  改端口/目录：改完重跑本脚本，或直接编辑 $APP_DIR/server.py 后 systemctl restart $SERVICE"
echo "========================================="
