from urllib.parse import urlparse


def normalize_domain(value: str) -> str:
    raw = (value or "").strip().lower()
    if not raw:
        return ""
    if "://" in raw:
        parsed = urlparse(raw)
        host = (parsed.hostname or "").strip().lower()
    else:
        host = raw.split(":", 1)[0].strip().lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def domain_variants(value: str) -> set[str]:
    base = normalize_domain(value)
    if not base:
        return set()
    return {base, f"www.{base}"}


def domains_equivalent(left: str, right: str) -> bool:
    l = normalize_domain(left)
    r = normalize_domain(right)
    return bool(l) and bool(r) and l == r

