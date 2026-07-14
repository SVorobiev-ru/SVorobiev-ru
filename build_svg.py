# -*- coding: utf-8 -*-
"""
Генератор профильного SVG для SVorobiev-ru/SVorobiev-ru.
Пересобирает dark_mode.svg и light_mode.svg: брайль-портрет из assets/braille_art.txt,
аптайм от 31.07.2001 и статистика GitHub через GraphQL API (с кэшем в cache/).

Логика работы с GitHub API адаптирована из https://github.com/Andrew6rant/Andrew6rant (today.py).

Запуск: ACCESS_TOKEN=<fine-grained PAT> USER_NAME=SVorobiev-ru python build_svg.py
Тест без API:  python build_svg.py --mock

Права для fine-grained PAT (read-only):
  Account permissions: read:Followers, read:Starring, read:Watching
  Repository access: All repositories
  Repository permissions: read:Commit statuses, read:Contents, read:Issues,
  read:Metadata, read:Pull Requests
"""
import calendar
import datetime
import hashlib
import os
import re
import sys

MOCK = "--mock" in sys.argv or not os.environ.get("ACCESS_TOKEN")

if not MOCK:
    import requests
    HEADERS = {"authorization": "token " + os.environ["ACCESS_TOKEN"]}

USER_NAME = os.environ.get("USER_NAME", "SVorobiev-ru")
BIRTHDAY = datetime.date(2001, 7, 31)
COMMENT_SIZE = 7
OWNER_ID = None


# ---------------------------------------------------------------- uptime
def daily_readme(birthday):
    t = datetime.date.today()
    years, months, days = t.year - birthday.year, t.month - birthday.month, t.day - birthday.day
    if days < 0:
        months -= 1
        pm = t.month - 1 or 12
        py = t.year if t.month > 1 else t.year - 1
        days += calendar.monthrange(py, pm)[1]
    if months < 0:
        years -= 1
        months += 12
    return f"{years} years, {months} months, {days} days"


def update_readme_cache_key():
    cache_key = os.environ.get("GITHUB_RUN_ID")
    if not cache_key:
        return

    with open("README.md", encoding="utf-8") as f:
        readme = f.read()

    pattern = r"((?:dark|light)_mode\.svg)(?:\?v=[^\"']*)?"
    updated = re.sub(pattern, lambda match: f"{match.group(1)}?v={cache_key}", readme)
    if updated != readme:
        with open("README.md", "w", encoding="utf-8") as f:
            f.write(updated)
        print(f"README.md: cache key обновлён ({cache_key})")


# ---------------------------------------------------------------- GitHub API
def simple_request(func_name, query, variables):
    r = requests.post("https://api.github.com/graphql",
                      json={"query": query, "variables": variables}, headers=HEADERS)
    if r.status_code == 200:
        return r
    raise Exception(func_name, " failed with", r.status_code, r.text)


def user_getter(username):
    query = '''
    query($login: String!){
        user(login: $login) { id createdAt }
    }'''
    r = simple_request("user_getter", query, {"login": username})
    return {"id": r.json()["data"]["user"]["id"]}


def follower_getter(username):
    query = '''
    query($login: String!){
        user(login: $login) { followers { totalCount } }
    }'''
    r = simple_request("follower_getter", query, {"login": username})
    return int(r.json()["data"]["user"]["followers"]["totalCount"])


def graph_repos_stars(count_type, owner_affiliation, cursor=None):
    query = '''
    query ($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String) {
        user(login: $login) {
            repositories(first: 100, after: $cursor, ownerAffiliations: $owner_affiliation) {
                totalCount
                edges { node { ... on Repository { nameWithOwner stargazers { totalCount } } } }
                pageInfo { endCursor hasNextPage }
            }
        }
    }'''
    variables = {"owner_affiliation": owner_affiliation, "login": USER_NAME, "cursor": cursor}
    r = simple_request("graph_repos_stars", query, variables)
    if count_type == "repos":
        return r.json()["data"]["user"]["repositories"]["totalCount"]
    return sum(n["node"]["stargazers"]["totalCount"]
               for n in r.json()["data"]["user"]["repositories"]["edges"])


def recursive_loc(owner, repo_name, data, cache_comment,
                  addition_total=0, deletion_total=0, my_commits=0, cursor=None):
    query = '''
    query ($repo_name: String!, $owner: String!, $cursor: String) {
        repository(name: $repo_name, owner: $owner) {
            defaultBranchRef {
                target {
                    ... on Commit {
                        history(first: 100, after: $cursor) {
                            totalCount
                            edges {
                                node {
                                    ... on Commit { committedDate }
                                    author { user { id } }
                                    deletions
                                    additions
                                }
                            }
                            pageInfo { endCursor hasNextPage }
                        }
                    }
                }
            }
        }
    }'''
    variables = {"repo_name": repo_name, "owner": owner, "cursor": cursor}
    r = requests.post("https://api.github.com/graphql",
                      json={"query": query, "variables": variables}, headers=HEADERS)
    if r.status_code == 200:
        if r.json()["data"]["repository"]["defaultBranchRef"] is not None:
            history = r.json()["data"]["repository"]["defaultBranchRef"]["target"]["history"]
            for node in history["edges"]:
                if node["node"]["author"]["user"] == OWNER_ID:
                    my_commits += 1
                    addition_total += node["node"]["additions"]
                    deletion_total += node["node"]["deletions"]
            if history["edges"] == [] or not history["pageInfo"]["hasNextPage"]:
                return addition_total, deletion_total, my_commits
            return recursive_loc(owner, repo_name, data, cache_comment,
                                 addition_total, deletion_total, my_commits,
                                 history["pageInfo"]["endCursor"])
        return 0
    force_close_file(data, cache_comment)
    raise Exception("recursive_loc() failed with", r.status_code, r.text)


def loc_query(owner_affiliation, comment_size=0, force_cache=False, cursor=None, edges=None):
    if edges is None:
        edges = []
    query = '''
    query ($owner_affiliation: [RepositoryAffiliation], $login: String!, $cursor: String) {
        user(login: $login) {
            repositories(first: 60, after: $cursor, ownerAffiliations: $owner_affiliation) {
                edges {
                    node {
                        ... on Repository {
                            nameWithOwner
                            defaultBranchRef { target { ... on Commit { history { totalCount } } } }
                        }
                    }
                }
                pageInfo { endCursor hasNextPage }
            }
        }
    }'''
    variables = {"owner_affiliation": owner_affiliation, "login": USER_NAME, "cursor": cursor}
    r = simple_request("loc_query", query, variables)
    payload = r.json()["data"]["user"]["repositories"]
    if payload["pageInfo"]["hasNextPage"]:
        return loc_query(owner_affiliation, comment_size, force_cache,
                         payload["pageInfo"]["endCursor"], edges + payload["edges"])
    return cache_builder(edges + payload["edges"], comment_size, force_cache)


def cache_filename():
    return "cache/" + hashlib.sha256(USER_NAME.encode("utf-8")).hexdigest() + ".txt"


def cache_builder(edges, comment_size, force_cache, loc_add=0, loc_del=0):
    cached = True
    os.makedirs("cache", exist_ok=True)
    filename = cache_filename()
    try:
        with open(filename, "r") as f:
            data = f.readlines()
    except FileNotFoundError:
        data = ["Кэш LOC. Не редактируйте вручную.\n"] * comment_size
        with open(filename, "w") as f:
            f.writelines(data)

    if len(data) - comment_size != len(edges) or force_cache:
        cached = False
        flush_cache(edges, filename, comment_size)
        with open(filename, "r") as f:
            data = f.readlines()

    cache_comment = data[:comment_size]
    data = data[comment_size:]
    for index in range(len(edges)):
        repo_hash, commit_count, *__ = data[index].split()
        if repo_hash == hashlib.sha256(edges[index]["node"]["nameWithOwner"].encode("utf-8")).hexdigest():
            try:
                if int(commit_count) != edges[index]["node"]["defaultBranchRef"]["target"]["history"]["totalCount"]:
                    owner, repo_name = edges[index]["node"]["nameWithOwner"].split("/")
                    loc = recursive_loc(owner, repo_name, data, cache_comment)
                    data[index] = (repo_hash + " "
                                   + str(edges[index]["node"]["defaultBranchRef"]["target"]["history"]["totalCount"])
                                   + f" {loc[2]} {loc[0]} {loc[1]}\n")
            except TypeError:
                data[index] = repo_hash + " 0 0 0 0\n"
    with open(filename, "w") as f:
        f.writelines(cache_comment)
        f.writelines(data)
    for line in data:
        loc = line.split()
        loc_add += int(loc[3])
        loc_del += int(loc[4])
    return [loc_add, loc_del, loc_add - loc_del, cached]


def flush_cache(edges, filename, comment_size):
    with open(filename, "r") as f:
        data = f.readlines()[:comment_size]
    with open(filename, "w") as f:
        f.writelines(data)
        for node in edges:
            f.write(hashlib.sha256(node["node"]["nameWithOwner"].encode("utf-8")).hexdigest() + " 0 0 0 0\n")


def force_close_file(data, cache_comment):
    with open(cache_filename(), "w") as f:
        f.writelines(cache_comment)
        f.writelines(data)


def commit_counter(comment_size):
    with open(cache_filename(), "r") as f:
        data = f.readlines()[comment_size:]
    return sum(int(line.split()[2]) for line in data)


# ---------------------------------------------------------------- SVG
RW = 68
FS, LH, CW = 12, 15, 7.23
X_ART, Y0 = 18, 26
ART_COLS = 46
ART_LINES = 27
INFO_PAD_TOP = 5
X_INFO = 18 + int(ART_COLS * CW) + 28
STATS_DIVIDER_COL = 40

PALETTES = {
    "dark": dict(bg="#0d1117", border="#30363d", fg="#e6edf3", orange="#ffa657",
                 blue="#79c0ff", grey="#8b949e", dim="#6e7681", green="#56d364", red="#f85149"),
    "light": dict(bg="#ffffff", border="#d0d7de", fg="#24292f", orange="#953800",
                  blue="#0969da", grey="#57606a", dim="#8c959f", green="#1a7f37", red="#cf222e"),
}


def fmt(n):
    return "{:,}".format(n) if isinstance(n, int) else str(n)


def row(label, value, vid=None):
    left = f". {label}: "
    d = max(1, RW - len(left) - len(value) - 1)
    return [(". ", "grey"), (label, "orange"), (": ", "grey"),
            ("." * d + " ", "dim"), (value, "blue", vid)]


def hdr(name):
    return [(name + " ", "orange"), ("─" * max(0, RW - len(name) - 1), "dim")]


def stats_left(label, value_parts):
    prefix = f". {label}: "
    value_len = sum(len(part[0]) for part in value_parts)
    dots = max(1, STATS_DIVIDER_COL - len(prefix) - value_len - 2)
    return [
        (". ", "grey"), (label, "orange"), (": ", "grey"),
        ("." * dots + " ", "dim"), *value_parts, (" | ", "grey"),
    ]


def stats_right(label, value, vid):
    width = RW - STATS_DIVIDER_COL - 2
    prefix = f"{label}: "
    dots = max(1, width - len(prefix) - len(value) - 1)
    return [
        (label, "orange"), (": ", "grey"), ("." * dots + " ", "dim"),
        (value, "blue", vid),
    ]


def repos_line(v_repo, v_star):
    left = stats_left("Repos", [(v_repo, "blue", "repo_data")])
    return left + stats_right("Stars", v_star, "star_data")


def commits_line(v_commit, v_fol):
    left = stats_left("Commits", [(v_commit, "blue", "commit_data")])
    return left + stats_right("Followers", v_fol, "follower_data")


def loc_line(v_net, v_add, v_del):
    left = stats_left("Lines of Code on GitHub", [(v_net, "blue", "loc_data")])
    return left + [
        (v_add + "+", "green", "loc_add"),
        (", ", "grey"), (v_del + "-", "red", "loc_del"),
    ]


def esc(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_svgs(stats):
    with open("assets/braille_art.txt", encoding="utf-8") as f:
        art = f.read().splitlines()[:ART_LINES]

    info = [
        hdr("Сергей Воробьев"),
        row("OS", "macOS Tahoe, iOS"),
        row("Uptime", stats["uptime"], "age_data"),
        row("IDE", "Ghostty, PhpStorm, Claude Code"),
        [],
        hdr("- Stack"),
        row("Frontend", "React, Next.js, Vue, Nuxt, TypeScript, Tailwind, SASS"),
        row("Backend", "PHP, Node.js, Python, C#, ASP.NET, MySQL, SQLite"),
        row("CMS", "1C-Bitrix, WordPress, ReadyScript"),
        row("AI", "Claude Code, Codex, Cursor, ChatGPT, DeepSeek, Recraft"),
        row("Tools", "Git, PhpStorm, Figma, Ghostty, MCP Servers"),
        [],
        hdr("- Contact"),
        row("Site", "svorobiev.ru"),
        row("Email", "mail@svorobiev.ru"),
        row("Telegram", "@svorobiev_ru"),
        [],
        hdr("- GitHub Stats"),
        repos_line(stats["repos"], stats["stars"]),
        commits_line(stats["commits"], stats["followers"]),
        loc_line(stats["loc_net"], stats["loc_add"], stats["loc_del"]),
    ]

    info_padded = [[]] * INFO_PAD_TOP + info
    n_lines = max(len(art), len(info_padded))
    width = X_INFO + int(RW * CW) + 18
    height = Y0 + n_lines * LH + 6

    for theme, P in PALETTES.items():
        parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
                 f'<rect width="{width}" height="{height}" rx="8" fill="{P["bg"]}" stroke="{P["border"]}"/>',
                 f'<g font-family="Menlo, Consolas, \'DejaVu Sans Mono\', monospace" font-size="{FS}px">']
        for i in range(n_lines):
            y = Y0 + i * LH
            if i < len(art):
                parts.append(f'<text x="{X_ART}" y="{y}" xml:space="preserve" fill="{P["fg"]}" '
                             f'textLength="{int(ART_COLS*CW)}" lengthAdjust="spacingAndGlyphs">{esc(art[i])}</text>')
            if i < len(info_padded) and info_padded[i]:
                spans = ""
                for seg in info_padded[i]:
                    text, role = seg[0], seg[1]
                    sid = f' id="{seg[2]}"' if len(seg) > 2 and seg[2] else ""
                    spans += f'<tspan{sid} fill="{P[role]}">{esc(text)}</tspan>'
                parts.append(f'<text x="{X_INFO}" y="{y}" xml:space="preserve">{spans}</text>')
        parts.append("</g></svg>")
        with open(f"{theme}_mode.svg", "w", encoding="utf-8") as f:
            f.write("\n".join(parts))
        print(f"{theme}_mode.svg записан ({width}x{height})")


if __name__ == "__main__":
    stats = {"uptime": daily_readme(BIRTHDAY)}
    if MOCK:
        print("MOCK-режим: статистика-заглушка, API не вызывается")
        stats.update(repos="95", stars="342", commits="2,116",
                     followers="196", loc_net="446,276", loc_add="523,178", loc_del="76,902")
    else:
        OWNER_ID = user_getter(USER_NAME)
        total_loc = loc_query(["OWNER", "COLLABORATOR", "ORGANIZATION_MEMBER"], COMMENT_SIZE)
        stats.update(
            repos=fmt(graph_repos_stars("repos", ["OWNER"])),
            stars=fmt(graph_repos_stars("stars", ["OWNER"])),
            commits=fmt(commit_counter(COMMENT_SIZE)),
            followers=fmt(follower_getter(USER_NAME)),
            loc_net=fmt(total_loc[2]), loc_add=fmt(total_loc[0]), loc_del=fmt(total_loc[1]),
        )
    build_svgs(stats)
    update_readme_cache_key()
    print("Готово. Uptime:", stats["uptime"])
