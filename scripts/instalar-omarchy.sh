#!/usr/bin/env bash
# Instala o Compras Andorinha no Omarchy (Arch) e abre o site.
#   curl -fsSL https://raw.githubusercontent.com/peterhbj/shopping-skill/main/scripts/instalar-omarchy.sh | bash
set -euo pipefail

REPO_URL="https://github.com/peterhbj/shopping-skill.git"
DEST="${SHOP_DIR:-$HOME/shopping-skill}"

say() { printf '\n\033[1;34m==>\033[0m %s\n' "$*"; }

say "Instalando dependências do sistema (pode pedir sua senha)"
pkgs=()
command -v git >/dev/null || pkgs+=(git)
command -v python3 >/dev/null || pkgs+=(python)
command -v codex >/dev/null || pkgs+=(openai-codex)
if ((${#pkgs[@]})); then
  sudo pacman -S --needed --noconfirm "${pkgs[@]}"
else
  echo "git, python e codex já instalados."
fi

if [[ -d "$DEST/.git" ]]; then
  say "Atualizando $DEST"
  git -C "$DEST" pull --ff-only
else
  say "Baixando o projeto em $DEST"
  git clone "$REPO_URL" "$DEST"
fi
cd "$DEST"

say "Preparando o Python"
[[ -d .venv ]] || python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

say "Baixando o navegador do Playwright"
.venv/bin/playwright install chromium

chmod +x scripts/abrir-compras.sh
if [[ -z "${SHOP_SKIP_DESKTOP:-}" ]]; then
  say "Criando o atalho “Compras Andorinha” no menu de apps"
  apps="$HOME/.local/share/applications"
  mkdir -p "$apps"
  cat > "$apps/compras-andorinha.desktop" <<DESKTOP
[Desktop Entry]
Type=Application
Name=Compras Andorinha
Comment=Lista de compras para o carrinho do Andorinha
Exec=$DEST/scripts/abrir-compras.sh
Icon=applications-office
Terminal=false
Categories=Utility;
DESKTOP
fi

say "Pronto! Abrindo o site. Comece pelo passo 1: os logins."
[[ -n "${SHOP_NO_OPEN:-}" ]] || exec "$DEST/scripts/abrir-compras.sh"
