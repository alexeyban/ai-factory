import subprocess

def commit_and_push():
    subprocess.run(["git", "add", "."])
    subprocess.run(["git", "commit", "-m", "AI commit"])
    subprocess.run(["git", "push"])