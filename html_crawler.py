#TODO: Choose to host locally or github pages (or Azure?) -- CALCULATE SPACE -- ASK OPS -- UPDATE READ.ME FOR SETTING UP
# #TODO: If files exist, ask for doing it again or not.
#TODO: Evaluate how to display the disaggregated figures

#TODO: Potentially add a common header
#TODO: Adapt style to mimic HumanitarianACtion

import os
import re
import csv
import json
import time
import hashlib
import logging

import requests
import sys
import traceback
import mimetypes
from urllib.parse import urlparse, urljoin, urldefrag
from bs4 import BeautifulSoup, NavigableString, Comment
from playwright.sync_api import sync_playwright

# =========================
# CONFIG  # paths, constants
# =========================

SAVE_MODE = "local"   # "local" or "github"
AUTO_GIT = True
GITHUB_LOCAL_REPO = os.path.dirname(os.path.abspath(__file__))

GITHUB_REPO = "UN-OCHA/legacy-hpc-projects" # only used if pushing directly trough the API

GITHUB_BRANCH = "main"
GITHUB_BASE_PATH = "docs"   # must match Pages config


GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")  # NEVER hardcode

INPUT_CSV = "input_projects_db.csv"
# Years to process (as strings because CSV values are strings)
FILTER_YEARS = {"2025"}   # example: {"2023", "2024", "2025"}`
# If empty or None → process all
# FILTER_YEARS = None

OUTPUT_DIR = "output"
PUBLISH_DIR = "docs"


DELAY = 0.0
BATCH_SIZE = 30
COOLDOWN = 0

LIMIT = 500000
OFFSET = 0
MAX_WORKERS = 10
SAVE_JSON = False
URL_COLUMN_INDEX = 0
DEBUG = False


DEBUG_DIR = os.path.join(OUTPUT_DIR, "debug")
SCREENSHOTS_DIR = os.path.join(OUTPUT_DIR, "screenshots")
JSON_DIR = os.path.join(OUTPUT_DIR, "json")
LOG_FILE = os.path.join(OUTPUT_DIR, "crawler.log")

PROJECTS_DIR = os.path.join(PUBLISH_DIR, "projects")
ASSETS_DIR = os.path.join(PUBLISH_DIR, "_assets")
ASSETS_SUBDIRS = {
    "css": os.path.join(ASSETS_DIR, "css"),
    "js": os.path.join(ASSETS_DIR, "js"),
    "img": os.path.join(ASSETS_DIR, "img"),
    "fonts": os.path.join(ASSETS_DIR, "fonts"),
    "misc": os.path.join(ASSETS_DIR, "misc"),
}
MANIFEST_PATH = os.path.join(ASSETS_DIR, "manifest_assets.json")



# =========================
# UTILS # helpers (hash, url, etc.)
# =========================

def log(msg):
    if DEBUG:
        print(msg)
    logging.info(msg)



def print_progress(processed, total, elapsed, eta, pid=None):
    pct = (processed / total) * 100 if total else 0

    eta_str = "estimating..." if processed == 0 else format_seconds(eta)

    msg = (
        f"\rProgress: {processed}/{total} "
        f"({pct:.1f}%) | elapsed: {format_seconds(elapsed)} "
        f"| ETA: {eta_str}"
    )

    if pid:
        msg += f" | current: {pid}"

    sys.stdout.write(msg)
    sys.stdout.flush()


def ensure_asset_dirs():
    os.makedirs(ASSETS_DIR, exist_ok=True)
    for p in ASSETS_SUBDIRS.values():
        os.makedirs(p, exist_ok=True)

def load_manifest():
    if os.path.exists(MANIFEST_PATH):
        try:
            with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def is_hpc_tools_domain(url: str) -> bool:
    try:
        host = urlparse(url).netloc.lower()
        return host.endswith("hpc.tools")
    except Exception:
        return False

def normalize_url(url: str, base: str = None) -> str:
    if base:
        url = urljoin(base, url)
    url, _ = urldefrag(url)
    return url

def sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()

def guess_extension(url: str, content_type: str = "") -> str:
    path = urlparse(url).path
    _, ext = os.path.splitext(path)
    if ext and len(ext) <= 10:
        return ext
    if content_type:
        ctype = content_type.split(";")[0].strip().lower()
        ext2 = mimetypes.guess_extension(ctype)
        if ext2:
            return ext2
    return ".bin"

def bucket_for(content_type: str, url: str, resource_hint: str = "") -> str:
    ctype = (content_type or "").split(";")[0].strip().lower()
    path = urlparse(url).path.lower()

    if resource_hint == "stylesheet" or ctype == "text/css" or path.endswith(".css"):
        return "css"
    if resource_hint == "script" or "javascript" in ctype or path.endswith(".js"):
        return "js"
    if resource_hint == "font" or ctype.startswith("font/") or any(path.endswith(x) for x in [".woff", ".woff2", ".ttf", ".otf", ".eot"]):
        return "fonts"
    if resource_hint == "image" or ctype.startswith("image/") or any(path.endswith(x) for x in [".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico"]):
        return "img"
    return "misc"

def save_manifest(manifest: dict):
    tmp = MANIFEST_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)
    os.replace(tmp, MANIFEST_PATH)

import subprocess

def git_commit_push(message="Update HPC project pages"):
    try:
        subprocess.run(
            ["git", "add", "docs"],
            cwd=GITHUB_LOCAL_REPO,
            check=True
        )

        # If nothing staged/changed, skip commit+push
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=GITHUB_LOCAL_REPO,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

        if not status:
            print("[git] no changes to commit")
            return

        # commit may fail if no changes, so ignore error
        subprocess.run(
            ["git", "commit", "-m", message],
            cwd=GITHUB_LOCAL_REPO,
        )

        subprocess.run(
            ["git", "push"],
            cwd=GITHUB_LOCAL_REPO,
            check=True
        )

    except Exception as e:
        print(f"[warn] git push failed: {e}")


class AssetStore:
    def __init__(self, manifest: dict):
        self.manifest = manifest  # remote_url -> local_rel ("_assets/js/<file>.js")

    def has(self, remote_url: str) -> bool:
        if remote_url not in self.manifest:
            return False
        return os.path.exists(os.path.join(OUTPUT_DIR, self.manifest[remote_url]))

    def save_bytes(self, remote_url: str, body: bytes, content_type: str = "", resource_hint: str = "") -> str:
        if self.has(remote_url):
            return self.manifest[remote_url]

        ext = guess_extension(remote_url, content_type)
        bucket = bucket_for(content_type, remote_url, resource_hint)
        fname = sha256_hex(remote_url) + ext

        abs_path = os.path.join(ASSETS_SUBDIRS[bucket], fname)
        local_rel = os.path.relpath(abs_path, OUTPUT_DIR).replace(os.sep, "/")

        tmp = abs_path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(body)
        os.replace(tmp, abs_path)

        self.manifest[remote_url] = local_rel
        return local_rel


def relpath_from_css(css_local_rel: str, target_local_rel: str) -> str:
    abs_css = os.path.join(OUTPUT_DIR, css_local_rel)
    abs_target = os.path.join(OUTPUT_DIR, target_local_rel)
    css_dir = os.path.dirname(abs_css)
    rel = os.path.relpath(abs_target, css_dir)
    return rel.replace(os.sep, "/")

def rewrite_css_file_urls(css_remote_url, css_local_rel, store):
    CSS_URL_RE = re.compile(r'url\(\s*(?P<q>["\']?)(?P<u>.*?)(?P=q)\s*\)', re.IGNORECASE)

    abs_path = os.path.join(OUTPUT_DIR, css_local_rel)

    if not os.path.exists(abs_path):
        return

    with open(abs_path, "r", encoding="utf-8", errors="ignore") as f:
        text = f.read()

    def replace_url(match):
        raw = match.group("u").strip()

        if not raw or raw.startswith("data:"):
            return match.group(0)

        abs_u = resolve_asset_url(raw, css_remote_url)

        # ✅ ensure asset exists (capture if missing)
        if is_hpc_tools_domain(abs_u):
            if abs_u not in store.manifest:
                try:
                    r = requests.get(abs_u, timeout=15)
                    if r.status_code == 200:
                        store.save_bytes(abs_u, r.content, r.headers.get("content-type", ""))
                except:
                    return match.group(0)

            if abs_u in store.manifest:
                local_rel = store.manifest[abs_u]
                rel = os.path.relpath(
                    os.path.join(OUTPUT_DIR, local_rel),
                    os.path.dirname(abs_path)
                )
                return f'url("{rel.replace(os.sep,"/")}")'

        return match.group(0)

    new_text = CSS_URL_RE.sub(replace_url, text)

    with open(abs_path, "w", encoding="utf-8") as f:
        f.write(new_text)

def relpath_from_projects(local_asset_rel: str) -> str:
    # HTML is saved in output/projects/<id>.html
    abs_asset = os.path.join(OUTPUT_DIR, local_asset_rel)
    rel = os.path.relpath(abs_asset, PROJECTS_DIR)
    return rel.replace(os.sep, "/")



def compute_document_base_url(rendered_html: str, page_url: str) -> str:
    """
    Returns the effective base URL browsers use to resolve relative URLs.
    Uses <base href="..."> if present; otherwise uses page_url. [1](https://developer.mozilla.org/en-US/docs/Web/HTML/Reference/Elements/base)
    """
    soup = BeautifulSoup(rendered_html, "html.parser")
    base_tag = soup.find("base")
    if base_tag and base_tag.get("href"):
        return urljoin(page_url, base_tag["href"])
    return page_url


def resolve_asset_url(raw_url: str, doc_base_url: str) -> str:
    """
    Resolve relative / root-relative URLs to absolute using the document base.
    Root-relative '/x' resolves from origin root. [2](https://stackoverflow.com/questions/11521011/why-base-tag-does-not-work-for-relative-paths)
    """
    if not raw_url:
        return raw_url
    if raw_url.startswith("//"):
        raw_url = "https:" + raw_url
    # urljoin handles:
    # - "styles.css" relative to doc base
    # - "/config.js" root-relative to origin
    return urljoin(doc_base_url, raw_url)

import base64

def github_upload_file(local_path, repo_path, message):
    if not GITHUB_TOKEN:
        raise RuntimeError("Missing GITHUB_TOKEN")

    with open(local_path, "rb") as f:
        content = base64.b64encode(f.read()).decode("utf-8")

    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{repo_path}"

    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json"
    }

    # ✅ check if file exists (to update)
    r = requests.get(url, headers=headers, params={"ref": GITHUB_BRANCH})
    sha = None

    if r.status_code == 200:
        sha = r.json().get("sha")

    payload = {
        "message": message,
        "content": content,
        "branch": GITHUB_BRANCH
    }

    if sha:
        payload["sha"] = sha

    r = requests.put(url, headers=headers, json=payload)
    r.raise_for_status()


# =========================
# HTML CLEANING # all remove_*, normalize_*,
# =========================

def rewrite_html_hpc_assets(rendered_html: str, page_url: str, store: AssetStore) -> str:
    soup = BeautifulSoup(rendered_html, "html.parser")

    # Compute effective base URL per <base href>. [1](https://developer.mozilla.org/en-US/docs/Web/HTML/Reference/Elements/base)
    base_tag = soup.find("base")
    if base_tag and base_tag.get("href"):
        doc_base_url = urljoin(page_url, base_tag["href"])
        base_tag.decompose()  # remove for offline correctness
    else:
        doc_base_url = page_url

    def localize(raw: str) -> str:
        if not raw or raw.startswith("data:"):
            return raw

        abs_u = resolve_asset_url(raw, doc_base_url)

        # ✅ now you can correctly test the domain
        if is_hpc_tools_domain(abs_u) and abs_u in store.manifest:
            return relpath_from_projects(store.manifest[abs_u])
        # log(f"[resolve] raw={raw} -> abs={abs_u} -> local={store.manifest.get(abs_u)}")

        return raw  # keep non-hpc.tools as-is (per your scope)

    # link[href]
    for tag in soup.find_all("link"):
        if tag.has_attr("href"):
            tag["href"] = localize(tag["href"])

    # script[src]
    for tag in soup.find_all("script"):
        if tag.has_attr("src"):
            tag["src"] = localize(tag["src"])

    # images/media
    for tag in soup.find_all(["img", "source", "video", "audio"]):
        if tag.has_attr("src"):
            tag["src"] = localize(tag["src"])
        if tag.has_attr("poster"):
            tag["poster"] = localize(tag["poster"])
        if tag.has_attr("srcset"):
            parts = []
            for part in tag["srcset"].split(","):
                part = part.strip()
                if not part:
                    continue
                bits = part.split()
                u0 = bits[0]
                desc = " ".join(bits[1:])
                u1 = localize(u0)
                parts.append((u1 + (" " + desc if desc else "")).strip())
            tag["srcset"] = ", ".join(parts)

    return str(soup)


def remove_runtime_js(soup):
    for s in soup.find_all("script"):
        src = s.get("src", "")

        # remove JS bundles + external scripts
        if (
            src.endswith(".js") or
            "googletagmanager" in src or
            "browser-update" in src
        ):
            s.decompose()

        # also remove inline scripts (safe for snapshot)
        elif not src:
            s.decompose()


def remove_all_styles(soup):
    for style in soup.find_all("style"):
        style.decompose()


def remove_unused_sections(soup):
    """
    Remove sections like:
      <h2>Tags</h2>
      <p></p>

    ONLY if:
      - the content block after the header is truly empty
      - no meaningful text
      - no images or nested data
    """

    HEADERS = ["h2", "h3", "h4"]

    for header in soup.find_all(HEADERS):
        # Get the next meaningful sibling
        content = header.find_next_sibling()

        if not content:
            continue

        # Check if content is empty
        is_empty = True

        # 1. Check text
        if content.get_text(strip=True):
            is_empty = False

        # 2. Check for meaningful tags (lists, tables, images)
        if content.find(["ul", "ol", "table", "img"]):
            is_empty = False

        # 3. Check nested non-empty divs
        for child in content.find_all(True):
            if child.get_text(strip=True):
                is_empty = False
                break

        # ✅ Remove ONLY if truly empty
        if is_empty:
            content.decompose()
            header.decompose()

def remove_comments(soup):
    for comment in soup.find_all(string=lambda text: isinstance(text, Comment)):
        comment.extract()


def remove_empty_tags(soup):
    for tag in soup.find_all():
        # skip self-closing or important structural tags
        if tag.name in ["br", "hr", "img", "input", "link", "meta"]:
            continue

        # no text + no non-empty children
        if not tag.get_text(strip=True) and not tag.find(
            lambda t: getattr(t, "name", None) in ["img", "iframe"]
        ):
            tag.decompose()


def strip_legacy_layout(soup):
    # ✅ Remove header
    for el in soup.find_all("app-header"):
        el.decompose()

    # ✅ Remove footer (Close bar)
    for el in soup.select("nav.navbar.fixed-bottom"):
        el.decompose()

    # ✅ Remove global header
    for el in soup.select(".global-header"):
        el.decompose()

    # ✅ ✅ REMOVE PROJECT TOOLBAR
    for el in soup.find_all("app-toolbar"):
        el.decompose()

    # remove empty placeholders
    for el in soup.find_all():
        if not el.text.strip() and not el.find_all("img"):
            # keep structure elements like div/section if needed
            pass



def collapse_wrappers(soup):
    # unwrap divs that only wrap one child and have no attributes
    for tag in list(soup.find_all("div")):
        if (
            not tag.attrs and
            len(tag.find_all(recursive=False)) == 1 and
            not tag.get_text(strip=True)
        ):
            tag.unwrap()

def simplify_classes(soup):
    # removes bootstrap spacing styles
    REMOVE_PREFIXES = ("pt-", "pb-", "ps-", "pe-", "mt-", "mb-", "ms-", "me-")

    for tag in soup.find_all(True):
        if "class" in tag.attrs:
            classes = tag["class"]
            cleaned = [
                c for c in classes
                if not c.startswith(REMOVE_PREFIXES)
            ]

            if cleaned:
                tag["class"] = cleaned
            else:
                del tag["class"]

def remove_disaggregation_notes(soup):
    for p in soup.find_all("p"):
        if "Includes disaggregation" in p.get_text():
            p.decompose()

def remove_meta_noise(soup):
    for meta in soup.find_all("meta"):
        if meta.get("http-equiv") in ["Cache-Control", "Pragma", "Expires"]:
            meta.decompose()


def remove_translate_attr(soup):
    for tag in soup.find_all(True):
        if "translate" in tag.attrs:
            del tag["translate"]


def simplify_tables(soup):
    for table in soup.find_all("table"):
        if "class" in table.attrs:
            table["class"] = ["table"]  # keep only main class


def normalize_locations(soup):
    container = soup.find("app-review-locations")
    if not container:
        return

    section = container.find("section")
    if not section:
        container.unwrap()
        return

    # ✅ STEP 1 — handle root country (first custom tag after h2)
    h2 = section.find("h2")
    if h2:
        root = h2.find_next("app-location-and-children-list")

        if root:
            # Extract text BEFORE the <ul>
            root_text = ""
            for child in root.children:
                if getattr(child, "name", None) == "ul":
                    break
                if isinstance(child, NavigableString):
                    t = child.strip()
                    if t:
                        root_text += t + " "

            root_text = root_text.strip()

            # Insert as paragraph after H2
            if root_text:
                p = soup.new_tag("p")
                p.string = root_text
                h2.insert_after(p)

            # Replace root node with its UL only (NO wrapping)
            ul = root.find("ul")
            if ul:
                root.replace_with(ul)
            else:
                root.decompose()

    # ✅ STEP 2 — unwrap ALL remaining custom tags (no structure creation)
    for tag in section.find_all("app-location-and-children-list"):
        tag.unwrap()

    # ✅ STEP 3 — remove wrapper
    container.unwrap()


def remove_browser_update_banner(soup):
    # Remove the banner container
    for el in soup.select("#buorg, .buorg"):
        el.decompose()

        # ✅ Remove browser-update script
    for s in soup.find_all("script"):
        src = s.get("src", "")
        if "browser-update.org" in src:
            s.decompose()

def optimize_html(soup):

    # ✅ 2. Remove Google fonts + external font preconnect
    # for link in soup.find_all("link"):
    #    href = link.get("href", "")
    #    if any(x in href for x in [
    #        "fonts.googleapis.com",
    #        "fonts.gstatic.com"
    #    ]):
    #        link.decompose()

    # ✅ 3. Remove scripts / analytics / noscript junk
    # for tag in soup(["script", "noscript", "iframe"]):
    #    tag.decompose()

    # ✅ 4. Remove Angular attributes (_ngcontent, etc.)
    for tag in soup.find_all(True):
      attrs = list(tag.attrs.keys())
      for attr in attrs:
          if (
                  attr.startswith("_ng") or
                  attr.startswith("ng-") or
                  attr == "ng-version"
          ):
                  del tag[attr]

    # ✅ 5. Remove Angular wrapper elements (but keep content)
    for tag_name in ["app-root", "router-outlet"]:
       for tag in soup.find_all(tag_name):
           tag.unwrap()

    for tag in list(soup.find_all()):
        if tag.name and tag.name.startswith("app-"):
            tag.unwrap()


def remove_empty_tags_strict(soup):
    """
    Remove elements that:
      - have NO attributes
      - have NO meaningful content
      - have NO important children (like <img>)
    """

    for tag in soup.find_all():
        # ✅ skip always-valid structural/self-closing tags
        if tag.name in ["br", "hr", "img", "input", "link", "meta"]:
            continue

        # ✅ must have NO attributes at all
        if tag.attrs:
            continue

        # ✅ check for meaningful text
        if tag.get_text(strip=True):
            continue

        # ✅ check for meaningful children (images, tables, lists)
        if tag.find(["img", "table", "ul", "ol", "iframe"]):
            continue

        # ✅ safe to remove
        tag.decompose()

def remove_font_faces(soup: object) -> None:
    for style in soup.find_all("style"):
        txt = style.string
        if not txt:
            continue

        # ✅ Remove ALL @font-face blocks
        new_txt = re.sub(r'@font-face\s*\{[^}]+\}', '', txt, flags=re.DOTALL)

        # ✅ Clean empty styles
        if new_txt.strip():
            style.string.replace_with(new_txt)
        else:
            style.decompose()


def clean_breaks(soup):
    for br in soup.find_all("br"):
        next_el = br.next_sibling
        if next_el and next_el.name == "br":
            br.decompose()



def minify_html(html):
    html = re.sub(r">\s+<", "><", html)   # remove gaps between tags
    html = re.sub(r"\s{2,}", " ", html)   # collapse spaces
    html = html.replace("\n", "")         # remove line breaks
    return html.strip()


def extract_common_css(soup, output_path):
    css_blocks = []

    for style in soup.find_all("style"):
        if style.string:
            css_blocks.append(style.string)

    # merge everything into one file
    combined_css = "\n\n".join(css_blocks)

    # remove Angular attribute selectors
    combined_css = re.sub(
        r'\[_ngcontent-[^\]]+\]',
        '',
        combined_css
    )

    css_file = os.path.join(ASSETS_DIR, "common_extract.css")

    os.makedirs(os.path.dirname(css_file), exist_ok=True)

    with open(css_file, "w", encoding="utf-8") as f:
        f.write(combined_css)

    return "common.css"


def inject_common_css(soup):
    head = soup.find("head")

    if head:
        link = soup.new_tag("link", rel="stylesheet", href="../_assets/common_manual.css")
        head.append(link)


def format_seconds(seconds):
    seconds = int(seconds)

    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60

    if h > 0:
        return f"{h}h {m:02d}m {s:02d}s"
    elif m > 0:
        return f"{m}m {s:02d}s"
    else:
        return f"{s}s"

# =========================
# FIXED READINESS LOGIC
# =========================
def detect_cloudflare(page):
    html = page.content().lower()

    return (
        "cf-browser-verification" in html or
        ("cloudflare" in html and "checking your browser" in html)
    )


def wait_for_render(page):
    """
    Minimal, robust readiness:
    - wait for network to settle
    - allow React to render
    """

    try:
        # wait for initial load + API fetches
        page.wait_for_load_state("networkidle", timeout=30000)
    except:
        pass

    # small settle loop (React paint)
    last_len = 0

    for _ in range(10):
        time.sleep(0.3)

        html_len = page.evaluate("document.body.innerHTML.length")

        if html_len == last_len and html_len > 1000:
            break

        last_len = html_len

    return True


# =========================
# ID extraction (unchanged)
# =========================
def extract_project_id(value):
    s = str(value)

    if re.fullmatch(r"\d+", s):
        return s

    m = re.search(r"/project/(\d+)/view", s)
    if m:
        return m.group(1)

    return None


def read_ids_google_analytics():
    #reads the ids from a google analytics format
    ids = []
    seen = set()

    with open(INPUT_CSV, newline="", encoding="utf-8") as f:
        for row in csv.reader(f):
            if len(row) <= URL_COLUMN_INDEX:
                continue

            pid = extract_project_id(row[URL_COLUMN_INDEX])
            if pid and pid not in seen:
                seen.add(pid)
                ids.append(pid)

    return ids

def read_ids():
    """
    Reads project IDs from CSV using the 'Id' column,
    optionally filtering by year.
    """
    ids = []
    seen = set()

    with open(INPUT_CSV, newline="", encoding="latin-1") as f:
        reader = csv.DictReader(f)

        for row in reader:
            year = row.get("Name")   # your CSV uses Name as year
            pid = row.get("Id")

            # ✅ Apply year filter
            if FILTER_YEARS and year not in FILTER_YEARS:
                continue

            if pid and pid not in seen:
                seen.add(pid)
                ids.append(pid)

    return ids

# =========================
# PIPELINE
# =========================

def clean_html(soup):
    """Applies all HTML cleanup steps in a clear order."""


    # --- remove noise --
    remove_comments(soup)
    remove_meta_noise(soup)
    strip_legacy_layout(soup)
    remove_runtime_js(soup)
    remove_browser_update_banner(soup)
    remove_font_faces(soup)
    remove_all_styles(soup)

    # --- clean up --
    # Remove disaggregation only if not applicable
    remove_disaggregation_notes(soup)
    remove_translate_attr(soup)
    remove_empty_tags(soup)
    normalize_locations(soup)
    remove_unused_sections(soup)
    remove_empty_tags_strict(soup)

    optimize_html(soup)
    inject_common_css(soup)

    # --- simplify --
    collapse_wrappers(soup)
    # simplify_classes(soup)
    # removes bootstrap spacing styles
    simplify_tables(soup)
    clean_breaks(soup)


# =========================
# CORE PROCESS
# =========================

def create_pages(context, n):
    return [context.new_page() for _ in range(n)]



def save_html(pid, html_content):
    local_path = os.path.join(PROJECTS_DIR, f"{pid}.html")

    # always save locally (optional but recommended)
    with open(local_path, "w", encoding="utf-8") as f:
        f.write(html_content)

def process_project(pid, page, session, store: AssetStore):
    # log(f"\n=== Project {pid} ===")

    url = f"https://projects.hpc.tools/project/{pid}/view"
    log(f"[nav] {url}")

    def on_response(resp):
        try:
            rurl = resp.url
            if not is_hpc_tools_domain(rurl):
                return
            # capture everything from hpc.tools except documents
            if resp.status != 200:
                return

            content_type = resp.headers.get("content-type", "")

            if not any(t in content_type for t in (
                    "css", "javascript", "image", "font", "svg"
            )):
                return

            body = resp.body()
            store.save_bytes(rurl, body, content_type, resource_hint=resp.request.resource_type)
        except Exception as e:
            log(f"[warn] response capture failed: {e}")
            return

    page.on("response", on_response)

    try:
        # --- 1. LOAD PAGE ---
        response = page.goto(url, wait_until="domcontentloaded", timeout=60000)

        status = response.status if response else "NO_RESPONSE"
        log(f"[response] status={status}")

        # --- 2. WAIT FOR REAL CONTENT ---
        try:
            page.wait_for_function("""
            () => {
                // block loader presence            
                const loader = document.querySelector(".project-page-loader");
                if (loader && getComputedStyle(loader).display !== "none") return false;
            
                const review = document.querySelector("app-review");
                return review && review.innerText.trim().length > 50;
            }
            """, timeout=30000)
        except:
            log("[warn] wait condition not fully met, continuing")

        # ✅ enough — skip networkidle (faster, avoids stalls)

        # --- 3. CAPTURE HTML ---
        raw_html = page.content()

        # --- 4. DEBUG (optional) ---
        if DEBUG:
            debug_path = os.path.join(DEBUG_DIR, f"debug_{pid}.html")
            os.makedirs(os.path.dirname(debug_path), exist_ok=True)
            with open(debug_path, "w", encoding="utf-8") as f:
                f.write(raw_html)

        # --- 5. SCREENSHOT SAMPLING ---
        if int(pid) % 300 == 0:
            screenshot_path = os.path.join(SCREENSHOTS_DIR, f"{pid}.png")
            page.screenshot(path=screenshot_path, full_page=True)

        # --- 6. REWRITE ASSETS (STRING) ---
        rewritten_html = rewrite_html_hpc_assets(raw_html, page.url, store)

        # --- 7. PARSE INTO SOUP ---
        soup = BeautifulSoup(rewritten_html, "html.parser")

        if not os.path.exists(os.path.join(ASSETS_DIR, "common.css")):
            extract_common_css(soup, ASSETS_DIR)

        # --- 8. CLEAN DOM ---
        clean_html(soup)

        # --- 9. FINAL HTML --
        # Minify only in the production ready version for clarity
        # final_html = minify_html(str(soup))
        final_html = str(soup)

        # --- 10. SAVE ---
        save_html(pid, final_html)
        log(f"[OK] {pid} saved")

    finally:
        page.remove_listener("response", on_response)

    if SAVE_JSON:
        # JSON after HTML success (your existing rule)
        api = f"https://api.hpc.tools/v2/public/project/{pid}?scope=all"

        for _ in range(3):
            try:
                r = session.get(api, timeout=30)
                r.raise_for_status()
                break
            except Exception as e:
                log(f"[warn] JSON retry: {e}")
        else:
            raise RuntimeError("JSON failed after retries")

        json_path = os.path.join(JSON_DIR, f"{pid}.json")
        with open(json_path, "wb") as f:
            f.write(r.content)
        log(f"[OK] JSON saved")

def run(ids, session):
    ensure_asset_dirs()
    manifest = load_manifest() or {}
    store = AssetStore(manifest)

    total = len(ids)
    start_time = time.time()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox", "--disable-dev-shm-usage"]
        )
        ctx = browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
            viewport={"width": 1366, "height": 768},
            locale="en-US",
        )
        pages = create_pages(ctx, MAX_WORKERS)

        try:
            last_manifest_size = 0
            for i, pid in enumerate(ids, start=1):

                elapsed = time.time() - start_time
                eta = (elapsed / i) * (total - i) if i > 0 else 0
                print_progress(i, total, elapsed, eta, pid)

                if len(store.manifest) > last_manifest_size:
                    save_manifest(store.manifest)
                    last_manifest_size = len(store.manifest)

                if i > 0 and i % BATCH_SIZE == 0:

                    if AUTO_GIT:
                        print("[git] committing batch...")
                        git_commit_push(f"Batch update: {i} projects")

                    time.sleep(COOLDOWN)

                if DELAY:
                    time.sleep(DELAY)

                try:
                    page = pages[i % len(pages)]
                    process_project(pid, page, session, store)
                except Exception:
                    logging.error(traceback.format_exc())

            if AUTO_GIT:
                git_commit_push("Final update")

        finally:
            ctx.close()
            browser.close()


def publish_index():
    html = """<!doctype html>
<html>
<head><title>HPC Projects</title></head>
<body>
<h1>HPC Projects Archive</h1>
<ul>
</ul>
</body>
</html>"""

    local_path = os.path.join(PUBLISH_DIR, "index.html")

    with open(local_path, "w", encoding="utf-8") as f:
        f.write(html)

    if SAVE_MODE == "github":
        github_upload_file(
            local_path,
            f"{GITHUB_BASE_PATH}/index.html",
            "Update index"
        )


# =========================
# MAIN
# =========================
def main():
    # ✅ Create output structure
    os.makedirs(PROJECTS_DIR, exist_ok=True)
    open(os.path.join(PUBLISH_DIR,".nojekyll"), "a").close() # _assets/ may disappear unless you add .nojekyll
    os.makedirs(SCREENSHOTS_DIR, exist_ok=True)
    os.makedirs(JSON_DIR, exist_ok=True)
    os.makedirs(ASSETS_DIR, exist_ok=True)
    os.makedirs(DEBUG_DIR, exist_ok=True)


    # ✅ Ensure directory exists BEFORE logging
    os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)

    logging.basicConfig(
        filename=LOG_FILE,
        filemode="a",
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )

    # ✅ Load IDs
    ids = read_ids()
    ids = ids[OFFSET: OFFSET + LIMIT]

    log(f"[filter] years: {FILTER_YEARS or 'ALL'}")
    log(f"[input] deduped IDs: {len(ids)}")

    # ✅ HTTP session (used for JSON API)
    publish_index()
    session = requests.Session()

    # ✅ Run pipeline
    run(ids, session)

    log("[done] completed run.")


if __name__ == "__main__":
    main()