"""Resolve explicit, unambiguous paths through open component shadow roots."""

import json


def element_expression(selector: str) -> str:
    if ">>>" not in selector:
        return f"document.querySelector({json.dumps(selector)})"
    parts = [part.strip() for part in selector.split(">>>")]
    if not all(parts):
        raise ValueError("Empty shadow selector segment")
    return """(() => {
      let root = document, element;
      const parts = PARTS;
      for (let i = 0; i < parts.length; i++) {
        const matches = root.querySelectorAll(parts[i]);
        if (matches.length !== 1) throw Error('Shadow control is unavailable or ambiguous');
        element = matches[0];
        if (i < parts.length - 1) {
          if (!element.shadowRoot) throw Error('Inspected shadow root disappeared');
          root = element.shadowRoot;
        }
      }
      return element;
    })()""".replace("PARTS", json.dumps(parts))
