import json
import os
import re
import subprocess
import threading

from flask import Flask, jsonify, render_template, request

app = Flask(__name__)

CONFIG_PATH = os.environ.get("CONFIG_PATH", "/data/config.json")
SCRIPT_PATH = os.environ.get("SCRIPT_PATH", "/data/backup.sh")
LOG_PATH = os.environ.get("LOG_PATH", "/data/backup.log")
DEFAULT_DEST = os.environ.get("DEFAULT_DEST", "/backup")
SOURCE_ROOTS = [r.strip() for r in os.environ.get("SOURCE_ROOTS", "/source").split(",")]
DEST_ROOTS = [r.strip() for r in os.environ.get("DEST_ROOTS", "/backup").split(",")]

backup_lock = threading.Lock()
backup_status = {"running": False, "pid": None}


def default_config():
    return {
        "dest": DEFAULT_DEST,
        "folders": []
    }


def load_config():
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH) as f:
            return json.load(f)
    # Bootstrap from existing script if one was mounted in
    return parse_existing_script()


def save_config(config):
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)
    generate_script(config)


def parse_existing_script():
    """Parse existing backup script to seed initial config."""
    config = default_config()
    if not os.path.exists(SCRIPT_PATH):
        return config

    with open(SCRIPT_PATH) as f:
        content = f.read()

    # Resolve variables used in the script
    vars_found = {}
    for match in re.finditer(r'^(\w+)="([^"$]+)"', content, re.MULTILINE):
        vars_found[match.group(1)] = match.group(2)

    def resolve(s):
        for k, v in vars_found.items():
            s = s.replace(f"${k}", v).replace(f"${{{k}}}", v)
        return s

    for match in re.finditer(r'rsync\s+-av\s+(--delete\s+)?"([^"]+)/"\s+"([^"]+)/"', content):
        delete_flag = bool(match.group(1))
        src = resolve(match.group(2))
        config["folders"].append({
            "path": src,
            "delete": delete_flag,
            "enabled": True
        })

    if "DEST" in vars_found:
        config["dest"] = vars_found["DEST"]

    return config


def generate_script(config):
    """Regenerate backup script from config."""
    dest = config["dest"]
    enabled_folders = [f for f in config["folders"] if f.get("enabled", True)]

    # Collect unique mount points to check
    mounts_to_check = set()
    for folder in enabled_folders:
        src = folder["path"]
        for root in SOURCE_ROOTS:
            if src == root or src.startswith(root + "/"):
                mounts_to_check.add(root)

    lines = [
        '#!/bin/bash',
        f'LOG="{dest}/backup.log"',
        f'DEST="{dest}"',
        '',
    ]

    for mount in sorted(mounts_to_check):
        lines.extend([
            f'if ! mountpoint -q "{mount}"; then',
            f'    mount "{mount}" 2>>"$LOG"',
            f'    if ! mountpoint -q "{mount}"; then',
            f'        echo "$(date): FAILED to mount {mount}" >> "$LOG"',
            f'        exit 1',
            f'    fi',
            'fi',
            ''
        ])

    lines.extend([
        'mkdir -p "$DEST"',
        'echo "$(date): Starting backup" >> "$LOG"',
        ''
    ])

    for folder in enabled_folders:
        src_path = folder["path"]
        # Destination subdir: strip the source root to get relative path
        dest_subdir = src_path
        for root in SOURCE_ROOTS:
            if src_path == root or src_path.startswith(root + "/"):
                dest_subdir = src_path[len(root):].lstrip("/")
                break
        delete = "--delete " if folder.get("delete", True) else ""
        lines.append(f'rsync -av {delete}"{src_path}/" "$DEST/{dest_subdir}/" >> "$LOG" 2>&1')

    lines.extend(['', 'echo "$(date): Backup complete" >> "$LOG"', ''])

    with open(SCRIPT_PATH, "w") as f:
        f.write('\n'.join(lines))
    os.chmod(SCRIPT_PATH, 0o755)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/browse")
def browse_dirs():
    """Browse directories for source or destination selection."""
    abs_path = request.args.get("path", "")
    scope = request.args.get("scope", "source")
    allowed_roots = SOURCE_ROOTS if scope == "source" else DEST_ROOTS

    if not abs_path:
        entries = []
        for root in allowed_roots:
            if os.path.isdir(root):
                entries.append({"name": root, "path": root})
        return jsonify({"path": "", "entries": entries})

    real = os.path.realpath(abs_path)
    if not any(real == os.path.realpath(r) or real.startswith(os.path.realpath(r) + "/") for r in allowed_roots):
        return jsonify({"error": "Path not allowed"}), 403

    if not os.path.isdir(abs_path):
        return jsonify({"error": "Path not found"}), 404

    entries = []
    try:
        for entry in sorted(os.scandir(abs_path), key=lambda e: e.name.lower()):
            if not entry.name.startswith("$") and not entry.name.startswith("."):
                try:
                    if entry.is_dir():
                        entries.append({
                            "name": entry.name,
                            "path": os.path.join(abs_path, entry.name)
                        })
                except PermissionError:
                    continue
    except PermissionError:
        return jsonify({"error": "Permission denied"}), 403

    return jsonify({"path": abs_path, "entries": entries})


@app.route("/api/config", methods=["GET"])
def get_config():
    config = load_config()
    return jsonify(config)


@app.route("/api/config", methods=["POST"])
def update_config():
    config = request.get_json()
    save_config(config)
    return jsonify({"ok": True})


@app.route("/api/folder", methods=["POST"])
def add_folder():
    data = request.get_json()
    config = load_config()

    existing_paths = {f["path"] for f in config["folders"]}
    if data["path"] in existing_paths:
        return jsonify({"error": "Folder already in backup list"}), 409

    config["folders"].append({
        "path": data["path"],
        "delete": data.get("delete", True),
        "enabled": True
    })
    save_config(config)
    return jsonify({"ok": True})


@app.route("/api/folder", methods=["DELETE"])
def remove_folder():
    data = request.get_json()
    config = load_config()
    config["folders"] = [f for f in config["folders"] if f["path"] != data["path"]]
    save_config(config)
    return jsonify({"ok": True})


@app.route("/api/folder/toggle", methods=["POST"])
def toggle_folder():
    data = request.get_json()
    config = load_config()
    for f in config["folders"]:
        if f["path"] == data["path"]:
            f["enabled"] = data.get("enabled", not f.get("enabled", True))
            break
    save_config(config)
    return jsonify({"ok": True})


@app.route("/api/folder/delete-mode", methods=["POST"])
def toggle_delete():
    data = request.get_json()
    config = load_config()
    for f in config["folders"]:
        if f["path"] == data["path"]:
            f["delete"] = data.get("delete", not f.get("delete", True))
            break
    save_config(config)
    return jsonify({"ok": True})


@app.route("/api/backup/run", methods=["POST"])
def run_backup():
    if backup_status["running"]:
        return jsonify({"error": "Backup already running"}), 409

    def do_backup():
        backup_status["running"] = True
        try:
            proc = subprocess.Popen(
                ["bash", SCRIPT_PATH],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT
            )
            backup_status["pid"] = proc.pid
            proc.wait()
        finally:
            backup_status["running"] = False
            backup_status["pid"] = None

    thread = threading.Thread(target=do_backup, daemon=True)
    thread.start()
    return jsonify({"ok": True, "message": "Backup started"})


@app.route("/api/backup/status")
def get_backup_status():
    last_log = ""
    if os.path.exists(LOG_PATH):
        with open(LOG_PATH) as f:
            lines = f.readlines()
            last_log = "".join(lines[-30:])

    return jsonify({
        "running": backup_status["running"],
        "log": last_log
    })


@app.route("/api/script")
def view_script():
    if os.path.exists(SCRIPT_PATH):
        with open(SCRIPT_PATH) as f:
            return jsonify({"script": f.read()})
    return jsonify({"script": ""})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
