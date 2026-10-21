#!/usr/bin/env bash
# 라즈베리파이에서 실행: My_Pie 서비스를 등록하고 (재)시작한다. 여러 번 실행해도 괜찮다.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -f config.toml ]; then
  cp config.example.toml config.toml
  echo "config.toml을 만들었어요."
fi

mkdir -p ~/.config/systemd/user
cp deploy/mypie-bot.service deploy/mypie-web.service deploy/mypie-stock.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --quiet mypie-bot mypie-web mypie-stock
systemctl --user restart mypie-bot mypie-web mypie-stock

if [ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" != "yes" ]; then
  echo
  echo "⚠️  지금은 SSH 접속이 끊기면 봇도 멈춰요."
  echo "    부팅할 때 자동 실행되게 하려면 한 번만 실행하세요: sudo loginctl enable-linger $USER"
fi

echo
systemctl --user --no-pager --lines=0 status mypie-bot mypie-web mypie-stock || true
