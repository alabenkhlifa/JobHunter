"""Render existing escaped alert HTML as text for the private Bot API transport."""
from html.parser import HTMLParser


def plain_html(value):
    class Text(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.parts = []
        def handle_data(self, data):
            self.parts.append(data)
    parser = Text()
    parser.feed(value)
    return ''.join(parser.parts)
