"""Read local requests and send text proposals to an open Resume Studio.

Examples:
    python -m resume_review.bridge status
    python -m resume_review.bridge propose --path '["flavors",0,"summary"]' \
        --replacement-file data/proposed-summary.txt --rationale 'Requested shorter opening'
"""

import argparse
import json
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


def call(url, route, body=None):
    parsed = urlsplit(url)
    if parsed.scheme != "http" or parsed.hostname not in ("localhost", "127.0.0.1"):
        raise ValueError("The review bridge only connects to the local review service.")
    data = json.dumps(body).encode() if body is not None else None
    request = Request(url.rstrip("/") + route, data=data,
                      headers={"Content-Type": "application/json", "X-Review-Client": "studio"})
    with urlopen(request, timeout=60) as response:
        return json.load(response)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8766")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    proposal = sub.add_parser("propose")
    proposal.add_argument("--path", required=True, help="JSON array identifying a catalog text field")
    proposal.add_argument("--replacement-file", type=Path, required=True)
    proposal.add_argument("--rationale", required=True)
    proposal.add_argument("--request-id")
    args = parser.parse_args()
    state = call(args.url, "/api/state")
    if args.command == "status":
        print(json.dumps({"revision": state["revision"], "updated_at": state["updated_at"],
                          "selection": state["selection"],
                          "open_requests": [r for r in state["requests"] if r["status"] == "open"],
                          "pending_proposals": [p for p in state["proposals"] if p["status"] == "pending"],
                          "approvals": state["approvals"]}, ensure_ascii=False, indent=2))
    else:
        updated = call(args.url, "/api/proposals", {"revision": state["revision"], "path": json.loads(args.path),
                      "after": args.replacement_file.read_text().rstrip("\n"), "rationale": args.rationale,
                      "request_id": args.request_id})
        print(json.dumps({"revision": updated["revision"], "proposal_id": updated["proposals"][-1]["id"],
                          "status": "pending-user-review"}))


if __name__ == "__main__":
    main()
