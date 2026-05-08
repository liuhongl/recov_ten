#!/usr/bin/env bash

set -e

PIP_INSTALL_CMD=${PIP_INSTALL_CMD:-"uv pip install --system"}

install_python_requirements() {
  local app_dir=$1

  echo "Starting Python dependencies installation..."

  if [[ -d "$app_dir/ten_packages/extension" ]]; then
    for extension in "$app_dir/ten_packages/extension"/*; do
      if [[ -d "$extension" && -f "$extension/requirements.txt" && -s "$extension/requirements.txt" ]]; then
        echo "Installing Python dependencies from $extension"
        ${PIP_INSTALL_CMD} -r "$extension/requirements.txt"
      fi
    done
  fi

  if [[ -d "$app_dir/ten_packages/system" ]]; then
    for extension in "$app_dir/ten_packages/system"/*; do
      if [[ -d "$extension" && -f "$extension/requirements.txt" && -s "$extension/requirements.txt" ]]; then
        echo "Installing Python dependencies from $extension"
        ${PIP_INSTALL_CMD} -r "$extension/requirements.txt"
      fi
    done
  fi

  echo "Python dependencies installation completed."
}

build_go_app() {
  local app_dir=$1

  cd "$app_dir"
  go run "$app_dir/ten_packages/system/ten_runtime_go/tools/build/main.go" --verbose
}

main() {
  APP_HOME=$(
    cd "$(dirname "$0")/.."
    pwd
  )

  echo "App root directory: $APP_HOME"

  if [[ ! -f "$APP_HOME/manifest.json" ]]; then
    echo "Error: manifest.json file not found"
    exit 1
  fi

  build_go_app "$APP_HOME"
  install_python_requirements "$APP_HOME"
}

main "$@"
