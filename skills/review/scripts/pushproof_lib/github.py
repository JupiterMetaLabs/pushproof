"""Read-only GitHub access for PR mode (only `gh pr view` and `git fetch`)."""
from .core import *  # noqa: F401,F403


def gh(args, timeout=60):
    if tuple(args[:2]) not in GH_READ_ONLY:
        return None, f"refused: pushproof is read-only on GitHub and does not run `gh {' '.join(args[:2])}`"
    try:
        r = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return None, "GitHub CLI `gh` is not installed"
    except subprocess.TimeoutExpired:
        return None, f"`gh {' '.join(args[:3])}` timed out"
    if r.returncode != 0:
        return None, (r.stderr.strip() or r.stdout.strip() or f"gh exited {r.returncode}")
    return r.stdout, None


def gh_json(args, timeout=60):
    out, err = gh(args, timeout)
    if err:
        return None, err
    try:
        return json.loads(out), None
    except json.JSONDecodeError:
        return None, "gh returned non-JSON output"


def pr_slug(pr):
    return f"{pr['host']}/{pr['owner']}/{pr['repo']}"


def pr_live_head(pr):
    data, err = gh_json(["pr", "view", str(pr["number"]), "--repo", pr_slug(pr), "--json", "headRefOid,state"])
    if err:
        return None, err
    return data["headRefOid"], None


def remote_for(owner, repo):
    want = f"{owner}/{repo}".lower()
    for name in git("remote").split():
        url = git("remote", "get-url", name).strip().lower()
        url = url[:-4] if url.endswith(".git") else url
        if url.endswith("/" + want) or url.endswith(":" + want):
            return name
    return None


def resolve_pr(arg):
    data, err = gh_json(["pr", "view", str(arg), "--json",
                         "number,url,title,state,baseRefName,headRefName,headRefOid,isCrossRepository"])
    if err:
        sys.exit(f"error: could not load PR {arg}: {err}")
    m = PR_URL.match(data["url"])
    if not m:
        sys.exit(f"error: unexpected PR url {data['url']}")
    host, owner, repo, num = m.groups()
    if data["state"] != "OPEN":
        print(f"WARNING: PR #{num} is {data['state']}.")
    remote = remote_for(owner, repo)
    if not remote:
        sys.exit(f"error: no git remote points at {owner}/{repo}; add one, e.g. "
                 f"`git remote add upstream https://{host}/{owner}/{repo}.git`")
    base_ref = f"refs/pushproof/pr/{num}/base"
    head_ref = f"refs/pushproof/pr/{num}/head"
    r = run_git(["fetch", "--quiet", "--no-tags", remote,
                 f"+refs/heads/{data['baseRefName']}:{base_ref}",
                 f"+refs/pull/{num}/head:{head_ref}"], check=False)
    if r.returncode != 0:
        sys.exit(f"error: fetching PR #{num} from {remote} failed: {r.stderr.strip()}")
    fetched = git("rev-parse", head_ref).strip()
    if fetched != data["headRefOid"]:
        sys.exit(f"error: PR #{num} head changed during fetch ({data['headRefOid'][:10]} -> "
                 f"{fetched[:10]}); run scope again.")
    return {"number": int(num), "url": data["url"], "title": data["title"], "host": host,
            "owner": owner, "repo": repo, "remote": remote, "base_name": data["baseRefName"],
            "head_name": data["headRefName"], "fork": data.get("isCrossRepository", False),
            "base_ref": base_ref}, fetched


def current_head(m):
    """(sha, error) for what the round is supposed to track: PR head or local HEAD."""
    if m.get("pr"):
        return pr_live_head(m["pr"])
    return git("rev-parse", "HEAD").strip(), None
