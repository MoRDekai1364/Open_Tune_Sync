import os
import sys
import subprocess
import tempfile
import shutil
import logging
import re
from datetime import datetime

SOURCE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMP_LOG = os.path.join(tempfile.gettempdir(), f"push_to_github_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")

logger = logging.getLogger("push_to_github")
logger.setLevel(logging.DEBUG)
file_handler = logging.FileHandler(TEMP_LOG, encoding="utf-8")
file_handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
logger.addHandler(file_handler)
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(logging.Formatter("%(message)s"))
console_handler.setLevel(logging.INFO)
logger.addHandler(console_handler)

def finalize_log():
    logs_dir = os.path.join(SOURCE_DIR, "logs")
    try:
        os.makedirs(logs_dir, exist_ok=True)
        dest = os.path.join(logs_dir, os.path.basename(TEMP_LOG))
        shutil.copy2(TEMP_LOG, dest)
        logger.info(f"Log copied to: {dest}")
        return dest
    except Exception as e:
        logger.error(f"Failed to copy log to source dir: {e}")
        return TEMP_LOG

def fail(message, code=1):
    logger.error(message)
    dest = finalize_log()
    logger.error(f"FAILED. Log path: {dest}")
    sys.exit(code)

def print_progress(percent, elapsed_seconds, done, total, prefix=""):
    bar_len = 30
    filled = int(bar_len * percent // 100)
    bar = "#" * filled + "." * (bar_len - filled)
    counts = f" {done}/{total}" if total else ""
    sys.stdout.write(f"\r{prefix}[{bar}] {percent}%{counts} | {elapsed_seconds:.0f}s elapsed   ")
    sys.stdout.flush()

def run(cmd, cwd=SOURCE_DIR, check=True):
    logger.debug(f"Running: {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    logger.debug(f"stdout: {result.stdout.strip()}")
    logger.debug(f"stderr: {result.stderr.strip()}")
    if check and result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return result

def check_git_available():
    try:
        run(["git", "--version"])
    except Exception:
        fail("git is not installed or not on PATH.")

def ensure_repo():
    if not os.path.isdir(os.path.join(SOURCE_DIR, ".git")):
        logger.info("No git repository found in this folder. Initializing.")
        run(["git", "init"])
        run(["git", "checkout", "-b", "main"])
    else:
        logger.info("Existing git repository detected.")

def get_remotes():
    result = run(["git", "remote", "-v"])
    remotes = {}
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            remotes[parts[0]] = parts[1]
    return remotes

def is_reachable_repo(url):
    result = run(["git", "ls-remote", url], check=False)
    return result.returncode == 0

def select_repo():
    remotes = get_remotes()
    if remotes:
        logger.info("Detected remotes:")
        names = list(remotes.keys())
        for i, name in enumerate(names, 1):
            logger.info(f"  {i}. {name} -> {remotes[name]}")
        logger.info(f"  {len(names)+1}. Enter a new repository URL")
        choice = input("Select repository: ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(names):
            name = names[int(choice) - 1]
            url = remotes[name]
            logger.info("Verifying repository access.")
            if not is_reachable_repo(url):
                fail(f"Could not reach repository: {url}")
            return name, url
    while True:
        url = input("Enter GitHub repository URL: ").strip()
        if not url:
            logger.info("No URL entered. Try again.")
            continue
        logger.info("Verifying repository access.")
        if not is_reachable_repo(url):
            logger.info(f"Could not reach '{url}'. Check the URL and permissions, then try again.")
            continue
        break
    if "origin" in remotes:
        run(["git", "remote", "set-url", "origin", url])
        return "origin", url
    run(["git", "remote", "add", "origin", url])
    return "origin", url

def get_local_branches():
    result = run(["git", "branch", "--list"])
    branches = []
    for line in result.stdout.splitlines():
        name = line.replace("*", "").strip()
        if name:
            branches.append(name)
    return branches

def get_remote_branches(remote_name):
    try:
        result = run(["git", "ls-remote", "--heads", remote_name])
    except Exception as e:
        logger.info(f"Could not list remote branches: {e}")
        return []
    branches = []
    for line in result.stdout.splitlines():
        match = re.search(r"refs/heads/(.+)$", line)
        if match:
            branches.append(match.group(1))
    return branches

def select_branch(remote_name):
    local_branches = get_local_branches()
    remote_branches = get_remote_branches(remote_name)
    all_branches = sorted(set(local_branches) | set(remote_branches))
    while True:
        if all_branches:
            logger.info("Detected branches:")
            for i, name in enumerate(all_branches, 1):
                logger.info(f"  {i}. {name}")
            logger.info(f"  {len(all_branches)+1}. Enter a new branch name")
            choice = input("Select branch: ").strip()
            if choice.isdigit() and 1 <= int(choice) <= len(all_branches):
                return all_branches[int(choice) - 1]
            if choice.isdigit() and int(choice) == len(all_branches) + 1:
                branch = input("Enter new branch name: ").strip()
            else:
                branch = choice
        else:
            branch = input("Enter branch name: ").strip()
        if not branch or not is_valid_branch_name(branch):
            logger.info("Invalid or empty branch name. Try again.")
            continue
        if branch in all_branches:
            return branch
        confirm = input(f"Branch '{branch}' does not exist. Create it? (y/n): ").strip().lower()
        if confirm == "y":
            return branch
        logger.info("Branch creation declined. Choose again.")

def is_valid_branch_name(name):
    result = run(["git", "check-ref-format", "--branch", name], check=False)
    return result.returncode == 0

def checkout_branch(branch):
    local_branches = get_local_branches()
    if branch in local_branches:
        run(["git", "checkout", branch])
    else:
        run(["git", "checkout", "-b", branch])

def stage_and_commit(message):
    run(["git", "add", "-A"])
    status = run(["git", "status", "--porcelain"])
    if not status.stdout.strip():
        logger.info("No changes to commit.")
        return False
    run(["git", "commit", "-m", message])
    return True

def push_with_progress(remote_name, branch):
    cmd = ["git", "push", "-u", remote_name, branch, "--progress"]
    logger.debug(f"Running: {' '.join(cmd)}")
    start_time = datetime.now()
    process = subprocess.Popen(cmd, cwd=SOURCE_DIR, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    last_percent = 0
    last_done = 0
    last_total = 0
    buffer = ""

    def process_line(line):
        nonlocal last_percent, last_done, last_total
        logger.debug(line.strip())
        elapsed = (datetime.now() - start_time).total_seconds()
        match = re.search(r"(\d{1,3})%\s*\((\d+)/(\d+)\)", line)
        if match:
            percent = min(100, int(match.group(1)))
            done = int(match.group(2))
            total = int(match.group(3))
            if percent >= last_percent:
                print_progress(percent, elapsed, done, total, prefix="Pushing: ")
                last_percent = percent
                last_done = done
                last_total = total
        elif re.search(r"\d{1,3}%", line):
            match2 = re.search(r"(\d{1,3})%", line)
            percent = min(100, int(match2.group(1)))
            if percent >= last_percent:
                print_progress(percent, elapsed, last_done, last_total, prefix="Pushing: ")
                last_percent = percent

    while True:
        char = process.stdout.read(1)
        if not char:
            break
        if char in ("\r", "\n"):
            if buffer.strip():
                process_line(buffer)
            buffer = ""
        else:
            buffer += char

    if buffer.strip():
        process_line(buffer)

    process.wait()
    total_elapsed = (datetime.now() - start_time).total_seconds()
    if last_percent < 100:
        print_progress(100, total_elapsed, last_total, last_total, prefix="Pushing: ")
    sys.stdout.write("\n")
    if process.returncode != 0:
        raise RuntimeError(f"git push exited with code {process.returncode}")
    logger.info(f"Push finished in {total_elapsed:.1f}s. Done.")

def main():
    logger.info(f"Source folder: {SOURCE_DIR}")
    logger.info(f"Temp log: {TEMP_LOG}")
    check_git_available()
    try:
        ensure_repo()
        remote_name, remote_url = select_repo()
        branch = select_branch(remote_name)
        checkout_branch(branch)
        message = input("Enter commit message: ").strip()
        if not message:
            fail("No commit message provided.")
        committed = stage_and_commit(message)
        push_with_progress(remote_name, branch)
    except Exception as e:
        fail(f"Error: {e}")
    dest = finalize_log()
    logger.info(f"Done. Remote: {remote_url} Branch: {branch} Log path: {dest}")

if __name__ == "__main__":
    main()
    input("\nPress Enter to close...")
