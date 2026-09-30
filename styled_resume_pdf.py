"""Dynamic two-column resume using the candidate's reviewed PDF design."""

import re
from pathlib import Path
from urllib.parse import urlsplit

from fpdf import FPDF


ASSETS = Path(__file__).resolve().parent / "assets" / "resume_template"
BLACK = (9, 9, 9)
BODY = (64, 64, 64)
GRAY = (80, 80, 80)
BLUE = (72, 140, 245)
COMPANY_BLUE = (22, 140, 255)
LIGHT_GRAY = (201, 201, 201)


class StyledResumePDF(FPDF):
    """Render tailored evidence into the established Inter/Rubik A4 layout.

    Layout uses PDF points, matching the reviewed resume's 24.5/383.7-point
    column positions. The two columns paginate independently.
    """

    MAIN_X = 24.5
    MAIN_W = 337.5
    SIDE_X = 383.7
    SIDE_W = 187.7
    BOTTOM = 792
    PAGE_TOP = 27
    SKILL_ROW_STEP = 19.0
    SKILL_CATEGORY_TOP = 22.0
    SKILL_CATEGORY_GAP = 3.0
    LANGUAGE_ROW_STEP = 22.0

    def __init__(self, profile):
        super().__init__(unit="pt", format=(595.92, 842.88))
        self.profile = profile
        self.set_auto_page_break(False)
        self.set_margins(0, 0, 0)
        self.add_font("Inter", "", str(ASSETS / "Inter-Regular-full.ttf"))
        self.add_font("Inter", "B", str(ASSETS / "Inter-Bold-full.ttf"))
        self.add_font("Rubik", "", str(ASSETS / "Rubik-Medium-full.ttf"))
        self.add_font("Rubik", "B", str(ASSETS / "Rubik-Bold-full.ttf"))
        self._side_schedule = {}
        self._sidebar_page_count = 1
        self._sidebar_final_page = 1
        self._sidebar_final_y = self._side_start(1)
        self._page_two_sidebar_after = 28

    @staticmethod
    def _web_link(value, *, linkedin=False, require_scheme=False):
        if not isinstance(value, str) or not value.strip():
            return None
        value = value.strip()
        if re.search(r"[\s<>]", value):
            return None
        if not re.match(r"^[a-z][a-z0-9+.-]*://", value, re.I):
            if require_scheme:
                return None
            value = "https://" + value
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower()
        if parsed.scheme not in ("http", "https") or not host or parsed.username or parsed.password:
            return None
        if linkedin and host not in ("linkedin.com", "www.linkedin.com"):
            return None
        if "." not in host or host.startswith(".") or host.endswith("."):
            return None
        return parsed._replace(scheme="https").geturl()

    def _font(self, family="Inter", style="", size=8):
        self.set_font(family, style, size)

    def _width(self, value, family="Inter", style="", size=8):
        self._font(family, style, size)
        return self.get_string_width(str(value))

    def _wrap(self, value, width, family="Inter", style="", size=8):
        """Wrap at words; split a single overlong token only as a last resort."""
        words = str(value or "").replace("\u00a0", " ").split()
        result, current = [], ""
        for word in words:
            candidate = f"{current} {word}".strip()
            if self._width(candidate, family, style, size) <= width:
                current = candidate
                continue
            if current:
                result.append(current)
                current = ""
            if self._width(word, family, style, size) <= width:
                current = word
                continue
            part = ""
            for char in word:
                if part and self._width(part + char, family, style, size) > width:
                    result.append(part)
                    part = ""
                part += char
            current = part
        if current:
            result.append(current)
        return result

    def _text(self, value, x, baseline, *, family="Inter", style="", size=8,
              color=BODY, link=None):
        self._font(family, style, size)
        self.set_text_color(*color)
        value = str(value)
        self.text(x, baseline, value)
        if link:
            self.link(x, baseline - size * 1.13,
                      self.get_string_width(value), size * 1.52, link)

    def _lines(self, value, x, baseline, width, *, family="Inter", style="",
               size=7.7, leading=9.5, color=BODY, link=None):
        wrapped = self._wrap(value, width, family, style, size)
        for index, line in enumerate(wrapped):
            self._text(line, x, baseline + index * leading, family=family,
                       style=style, size=size, color=color, link=link)
        return baseline + len(wrapped) * leading

    def _rule(self, x1, x2, y, *, thick=False, dashed=False):
        self.set_draw_color(*(BLACK if thick else LIGHT_GRAY))
        self.set_line_width(2.2 if thick else 0.45)
        if dashed:
            self.set_dash_pattern(dash=2.1, gap=2.1)
        self.line(x1, y, x2, y)
        if dashed:
            self.set_dash_pattern()

    def _section(self, title, x1, x2, baseline):
        self._text(title.upper(), x1, baseline - 1.5, family="Rubik", style="B",
                   size=10.3, color=BLACK)
        self._rule(x1, x2, baseline + 2, thick=True)

    def _image(self, path, x, y, *, width, height=None):
        source = ASSETS / path
        if source.exists():
            self.image(str(source), x=x, y=y, w=width, h=height or 0,
                       keep_aspect_ratio=True)

    def _identity(self):
        p = self.profile
        self._text(str(p.get("name", "")).upper(), self.MAIN_X, 42,
                   family="Rubik", style="B", size=20, color=BLACK)
        headline = p.get("headline") or ""
        self._lines(headline, self.MAIN_X, 56, self.w - self.MAIN_X * 2,
                    family="Rubik", style="B", size=10.8, leading=11.5,
                    color=BLUE)

        website = p.get("website")
        email = p.get("email")
        linkedin = p.get("linkedin")
        location = str(p.get("location") or "").replace("\u00b7", "-")
        contacts = []
        if website:
            label = re.sub(r"^https?://", "", str(website)).rstrip("/")
            contacts.append(("web", label, self._web_link(website)))
        if email:
            valid = re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", str(email))
            contacts.append(("@", email, f"mailto:{email}" if valid else None))
        if linkedin:
            contacts.append(("in", linkedin, self._web_link(linkedin, linkedin=True)))
        if location:
            contacts.append(("loc", location, None))
        if not website and p.get("phone"):
            contacts.insert(0, ("tel", p["phone"], None))

        total = sum(self._width(label, "Inter", "", 7.5) + (11 if icon != "web" else 13)
                    for icon, label, _ in contacts)
        gap = min(15.0, max(5.0, (self.w - 2 * self.MAIN_X - total) /
                             max(1, len(contacts) - 1)))
        x, baseline = self.MAIN_X, 70.1
        for icon, label, link in contacts:
            label = str(label)
            icon_width = 11 if icon != "web" else 13
            if x + icon_width + self._width(label, "Inter", "", 7.5) > self.w - self.MAIN_X:
                x, baseline = self.MAIN_X, baseline + 12
            if icon == "web":
                self.set_draw_color(*COMPANY_BLUE)
                self.set_line_width(0.9)
                self.ellipse(x + 1, baseline - 5.4, 6, 6)
                self.line(x + 1, baseline - 2.4, x + 7, baseline - 2.4)
            elif icon == "loc":
                self.set_draw_color(*COMPANY_BLUE)
                self.set_line_width(0.9)
                self.ellipse(x + 1, baseline - 5.3, 5.5, 5.5)
                self.line(x + 1.8, baseline - 0.8, x + 3.8, baseline + 2.5)
            else:
                self._text(icon, x, baseline + 0.45, family="Inter", style="B",
                           size=8.5, color=COMPANY_BLUE)
            self._text(label, x + icon_width, baseline, family="Inter", style="",
                       size=7.5, link=link)
            x += icon_width + self._width(label, "Inter", "", 7.5) + gap
        return baseline

    def header(self):
        if self.page_no() == 1:
            self._identity()
        for command in self._side_schedule.get(self.page_no(), []):
            self._draw_side_command(command)

    def _side_add(self, page, kind, y, **data):
        self._side_schedule.setdefault(page, []).append((kind, y, data))

    def _side_start(self, page):
        if page == 1:
            return 93.5
        if page == 2:
            return self._page_two_sidebar_after
        return 28

    def _side_next(self, page, y, required):
        if y + required <= self.BOTTOM:
            return page, y
        page += 1
        return page, self._side_start(page)

    def _skill_rows(self, values):
        rows, row, used = [], [], 0.0
        seen = set()
        for raw in values:
            # The reviewed design displays AWS, Azure and RAG as compact tags;
            # the source JSON and experience keywords keep their full detail.
            skill = re.sub(r"\s*\([^)]*\)", "", str(raw)).strip()
            if not skill or skill.casefold() in seen:
                continue
            seen.add(skill.casefold())
            tag_width = self._width(skill, "Inter", "B", 7.5) + 12
            if tag_width > self.SIDE_W:
                if row:
                    rows.append(row)
                    row, used = [], 0
                for line in self._wrap(skill, self.SIDE_W - 12, "Inter", "B", 7.5):
                    rows.append([line])
                continue
            if row and used + 6 + tag_width > self.SIDE_W:
                rows.append(row)
                row, used = [], 0
            row.append(skill)
            used += tag_width + (6 if used else 0)
        if row:
            rows.append(row)
        return rows

    def _parse_languages(self):
        value = (self.profile.get("additional") or {}).get("languages") or ""
        if isinstance(value, list):
            entries = value
        else:
            entries = re.split(r"\s*[|\u00b7;,]\s*", str(value))
        result = []
        for entry in entries:
            if isinstance(entry, dict):
                name, level = entry.get("name"), entry.get("level")
            else:
                match = re.match(r"\s*([^()]+?)\s*\(([^)]+)\)\s*$", str(entry))
                if not match:
                    continue
                name, level = match.groups()
            if name and level:
                result.append((str(name).strip(), str(level).strip()))
        order = {"arabic": 0, "english": 1, "french": 2}
        return sorted(result, key=lambda item: order.get(item[0].lower(), 3))

    def _education_layout(self, item, width):
        degree = str(item.get("degree") or "")
        school = str(item.get("school") or "")
        if item.get("location") and str(item["location"]).lower() not in school.lower():
            school += ", " + str(item["location"])
        degree_lines = self._wrap(degree, width - 25, "Rubik", "B", 9.15)
        school_lines = self._wrap(school, width - 25, "Rubik", "B", 8.8)
        required = 13 + 11.4 * len(degree_lines) + 11.2 * len(school_lines) + 18
        return {
            "degree": degree_lines,
            "school": school_lines,
            "dates": str(item.get("dates") or ""),
            "image": self._school_image(item),
        }, required

    def _plan_language_education(self):
        page, y = 2, 28
        languages = self._parse_languages()
        education = self.profile.get("education") or []
        if languages:
            self._side_add(page, "section", y, title="Languages")
            y += 17
            for name, level in languages:
                page, y = self._side_next(page, y, self.LANGUAGE_ROW_STEP)
                self._side_add(page, "language", y, name=name, level=level)
                y += self.LANGUAGE_ROW_STEP
            y += 10
        if education:
            page, y = self._side_next(page, y, 25)
            self._side_add(page, "section", y, title="Education")
            y += 20
            for item in education:
                data, required = self._education_layout(item, self.SIDE_W)
                page, y = self._side_next(page, y, required)
                self._side_add(page, "education", y, **data)
                y += required
        self._page_two_sidebar_after = y + 18 if page == 2 else 28
        return bool(languages or education), page

    def _experience_fits_first_page(self):
        summary_lines = len(self._wrap(self.profile.get("summary") or "",
                                       self.MAIN_W, size=7.6))
        start = 93.5 + 18 + summary_lines * 9.5 + 8 + 22.5
        roles = self.profile.get("experience") or []
        required = 0
        for index, role in enumerate(roles):
            with_divider = index < len(roles) - 1
            engagements = role.get("engagements") or []
            if engagements:
                required += self._group_header_height(role)
                required += sum(self._engagement_height(
                    item, with_divider=child_index < len(engagements) - 1,
                ) for child_index, item in enumerate(engagements))
                required += 25 if with_divider else 0
            else:
                required += self._role_height(role, with_divider=with_divider)
        return start + required + 10 <= self.BOTTOM

    def _plan_sidebar(self):
        personal_details, details_end_page = self._plan_language_education()
        page, y = 1, self._side_start(1)
        skills = self.profile.get("skills") or {}
        if skills:
            self._side_add(page, "section", y, title="Skills")
            y += 14.2
            for category, values in skills.items():
                entries = values if isinstance(values, list) else [values]
                rows = self._skill_rows(entries)
                if not rows:
                    continue
                required = (self.SKILL_CATEGORY_TOP + self.SKILL_ROW_STEP * len(rows)
                            + self.SKILL_CATEGORY_GAP)
                page, y = self._side_next(page, y, required)
                self._side_add(page, "skill_category", y,
                               title=str(category), rows=rows)
                y += required
        certifications = self.profile.get("certifications") or []
        if certifications:
            credentials = [(str(item), self._wrap(item, self.SIDE_W,
                                                   "Rubik", "B", 8.8))
                           for item in certifications]
            group_height = 17 + sum(10.2 * len(lines) + 18.4
                                    for _, lines in credentials)
            y += 8
            # Keep the entire credential list with its heading when it fits
            # on a fresh page. The reviewed PDF never strands a few entries.
            if y + group_height > self.BOTTOM and group_height <= self.BOTTOM - self._side_start(page + 1):
                page += 1
                y = self._side_start(page)
            self._side_add(page, "section", y, title="Certifications")
            y += 17
            links = self.profile.get("certification_links") or {}
            for title, wrapped in credentials:
                issuer = self._issuer(title)
                required = 10.2 * len(wrapped) + 18.4
                previous_page = page
                page, y = self._side_next(page, y, required)
                if page != previous_page:
                    self._side_add(page, "section", y, title="Certifications")
                    y += 17
                link = self._web_link(links.get(title), require_scheme=True)
                self._side_add(page, "certification", y, title=title,
                               lines=wrapped, issuer=issuer, link=link)
                y += required

        # Keep short resumes on one page when the whole personal-details block
        # fits beneath skills and credentials. Longer sidebars retain page two.
        if (personal_details and page == 1 and details_end_page == 2
                and self._experience_fits_first_page()):
            details_end_y = self._page_two_sidebar_after - 18
            shift = y + 14 - 28
            if shift + details_end_y <= self.BOTTOM:
                for kind, command_y, data in self._side_schedule.pop(2, []):
                    self._side_add(1, kind, command_y + shift, **data)
                details_end_page = 1

        self._sidebar_final_page = page
        self._sidebar_final_y = y
        self._sidebar_page_count = max(page, details_end_page if personal_details else 1)

    def _language_commands(self, start_y):
        commands = []
        y = start_y
        languages = self._parse_languages()
        if languages:
            commands.append(("section", y, {"title": "Languages"}))
            y += 17
            for name, level in languages:
                commands.append(("language", y, {"name": name, "level": level}))
                y += self.LANGUAGE_ROW_STEP
        return commands, y

    def _education_commands(self, start_y, width):
        commands = []
        y = start_y
        education = self.profile.get("education") or []
        if education:
            commands.append(("section", y, {"title": "Education"}))
            y += 20
            for item in education:
                data, required = self._education_layout(item, width)
                commands.append(("education", y, data))
                y += required
        return commands, y

    def _main_personal_details(self, start_y):
        """Plan a full-width block beneath Experience without drawing it."""
        languages, y = self._language_commands(start_y)
        education, end_y = self._education_commands(
            y + (10 if languages else 0), self.MAIN_W)
        return [(command, self.MAIN_X, self.MAIN_W)
                for command in languages + education], end_y

    def _compact_main_personal_details(self, start_y):
        """Fit Languages and Education beside each other when both exist."""
        language_entries = self._parse_languages()
        if not language_entries or not self.profile.get("education"):
            return [], self.BOTTOM + 1
        language_width = max(130, max(
            self._width(name, "Rubik", "B", 9.1)
            + self._width(level, size=8.8) + 12
            for name, level in language_entries
        ))
        education_x = self.MAIN_X + language_width + 14
        education_width = self.MAIN_W - language_width - 14
        if education_width < 150:
            return [], self.BOTTOM + 1
        languages, language_end = self._language_commands(start_y)
        education, education_end = self._education_commands(start_y, education_width)
        commands = ([(command, self.MAIN_X, language_width) for command in languages]
                    + [(command, education_x, education_width) for command in education])
        return commands, max(language_end, education_end)

    def _split_personal_details_across_columns(self, start_y):
        """Place education below Experience and a short language line after sidebar content."""
        if self._sidebar_final_page != 1:
            return [], [], self.BOTTOM + 1
        education, education_end = self._education_commands(start_y, self.MAIN_W)
        languages = self._parse_languages()
        if not education or not languages:
            return [], [], self.BOTTOM + 1
        language_text = "  |  ".join(f"{name}: {level}" for name, level in languages)
        lines = self._wrap(language_text, self.SIDE_W, size=8.3)
        side_y = self._sidebar_final_y + 14
        side_commands = [
            ("section", side_y, {"title": "Languages"}),
            ("compact_languages", side_y + 17, {"lines": lines}),
        ]
        side_end = side_y + 17 + 10.8 * len(lines) + 5
        return education, side_commands, max(education_end, side_end)

    def _second_page_only_has_personal_details(self):
        if self._sidebar_page_count != 2:
            return False
        commands = self._side_schedule.get(2, [])
        return bool(commands) and all(
            kind in ("language", "education")
            or kind == "section" and data["title"] in ("Languages", "Education")
            for kind, _, data in commands
        )

    @staticmethod
    def _issuer(title):
        name = title.lower()
        if "aws" in name:
            return "AWS"
        if "azure" in name or "microsoft" in name:
            return "Microsoft Azure"
        if "spring" in name:
            return "VMware"
        if "isaqb" in name:
            return "iSAQB"
        if "scrum" in name:
            return "Scrum.org"
        if "claude" in name:
            return "Anthropic"
        return ""

    @staticmethod
    def _school_image(item):
        school = str(item.get("school") or "").lower()
        if "esprit" in school:
            return "esprit.png"
        if "isimm" in school:
            return "isimm.png"
        return None

    def _draw_side_command(self, command, *, x=None, width=None):
        kind, y, data = command
        x = self.SIDE_X if x is None else x
        right = x + (self.SIDE_W if width is None else width)
        if kind == "section":
            self._section(data["title"], x, right, y)
        elif kind == "skill_category":
            self._text(data["title"], x, y, family="Rubik", style="B",
                       size=8.8, color=BLUE)
            baseline = y + self.SKILL_CATEGORY_TOP
            for row in data["rows"]:
                tag_x = x + 5.5
                for skill in row:
                    width = self._width(skill, "Inter", "B", 7.5)
                    self._text(skill, tag_x, baseline, family="Inter", style="B",
                               size=7.5, color=BODY)
                    self._rule(tag_x, tag_x + width, baseline + 3)
                    tag_x += width + 18
                baseline += self.SKILL_ROW_STEP
            self._rule(x, right,
                       y + self.SKILL_CATEGORY_TOP +
                       self.SKILL_ROW_STEP * (len(data["rows"]) - 1) + 9,
                       dashed=True)
        elif kind == "certification":
            for index, line in enumerate(data["lines"]):
                self._text(line, x, y + 10 + 10.2 * index, family="Rubik",
                           style="B", size=8.8, color=BLACK, link=data["link"])
            bottom = y + 10.2 * len(data["lines"])
            if data["issuer"]:
                self._text(data["issuer"], x, bottom + 11, size=8.4, color=GRAY)
            self._rule(x, right, y + 10.2 * len(data["lines"]) + 15.5,
                       dashed=True)
        elif kind == "language":
            self._text(data["name"], x, y + 8, family="Rubik", style="B",
                       size=9.1, color=BLACK)
            self._text(data["level"], right - self._width(data["level"], size=8.8),
                       y + 8, size=8.8, color=GRAY)
            self._rule(x, right, y + 19, dashed=True)
        elif kind == "compact_languages":
            for index, line in enumerate(data["lines"]):
                self._text(line, x, y + index * 10.8, size=8.3)
        elif kind == "education":
            if data["image"]:
                self._image(data["image"], x, y + 1, width=20, height=20)
            text_x = x + 25
            baseline = y + 10
            for line in data["degree"]:
                self._text(line, text_x, baseline, family="Rubik", style="B",
                           size=9.15, color=BLACK)
                baseline += 11.4
            baseline += 1.5
            for line in data["school"]:
                self._text(line, text_x, baseline, family="Rubik", style="B",
                           size=8.8, color=BLUE)
                baseline += 11.2
            if data["dates"]:
                self._text(data["dates"].replace("\u2013", "-"), text_x + 11,
                           baseline + 0.5, size=8.3, color=GRAY)
            self._rule(x, right, y + 13 + 11.4 * len(data["degree"]) +
                       11.2 * len(data["school"]) + 13, dashed=True)

    @staticmethod
    def _company_image(company):
        name = str(company).lower()
        if "maibornwolff" in name:
            return "maibornwolff.png"
        if "verse" in name:
            return "verse.png"
        if "talan" in name:
            return "talan.png"
        return None

    def _new_experience_page(self):
        self.add_page()
        self._section("Experience", self.MAIN_X, self.MAIN_X + self.MAIN_W,
                      self.PAGE_TOP)
        return self.PAGE_TOP + 22.5

    def _role_header(self, role, y):
        image = self._company_image(role.get("company"))
        if image:
            width = 34 if image == "talan.png" else 20
            height = 10 if image == "talan.png" else 20
            self._image(image, self.MAIN_X, y - 10, width=width, height=height)
        text_x = self.MAIN_X + (40 if image == "talan.png" else 25.5)
        title_lines = self._wrap(role.get("title") or "",
                                 self.MAIN_X + self.MAIN_W - text_x,
                                 "Rubik", "", 9.1)
        for index, line in enumerate(title_lines):
            self._text(line, text_x, y + index * 10.5,
                       family="Rubik", size=9.1, color=BLACK)
        company = str(role.get("company") or "")
        subtitle = role.get("subtitle")
        if subtitle and subtitle.lower() not in company.lower():
            company += " - " + str(subtitle)
        company_lines = self._wrap(company, self.MAIN_X + self.MAIN_W - text_x,
                                   "Rubik", "B", 8.2)
        baseline = y + 13.4 + max(0, len(title_lines) - 1) * 10.5
        company_link = self._web_link(role.get("company_url"), require_scheme=True)
        for line in company_lines:
            self._text(line, text_x, baseline, family="Rubik", style="B",
                       size=8.2, color=COMPANY_BLUE, link=company_link)
            baseline += 10.2
        detail = "   |   ".join(filter(None,
            (str(role.get("dates") or "").replace("\u2013", "-"), role.get("location"))))
        self._text(detail, text_x, baseline + 2, size=7.35, color=GRAY)
        return baseline + 14

    def _bullet(self, value, y):
        lines = self._wrap(value, self.MAIN_X + self.MAIN_W - 61.5,
                           size=7.7)
        self.set_fill_color(*BODY)
        self.ellipse(52.0, y - 3.2, 2.1, 2.1, style="F")
        for line in lines:
            self._text(line, 61.5, y, size=7.7)
            y += 9.4
        return y + 1.4

    def _keywords(self, value, y):
        if not value:
            return y
        terms = re.sub(r"\s*[\u00b7|]\s*", ", ", str(value))
        return self._lines("Keywords: " + terms,
                           61.5, y, self.MAIN_X + self.MAIN_W - 61.5,
                           size=7.2, leading=8.9, color=GRAY) + 1

    def _role_height(self, role, *, with_divider):
        company = str(role.get("company") or "")
        subtitle = role.get("subtitle")
        if subtitle and subtitle.lower() not in company.lower():
            company += " - " + str(subtitle)
        title_lines = len(self._wrap(role.get("title") or "", 312,
                                     "Rubik", "", 9.1))
        company_lines = len(self._wrap(company, 312, "Rubik", "B", 8.2))
        height = 27.4 + max(0, title_lines - 1) * 10.5 + company_lines * 10.2
        for bullet in role.get("bullets") or []:
            height += len(self._wrap(bullet, 300.5, size=7.7)) * 9.4 + 1.4
        if role.get("tech"):
            terms = re.sub(r"\s*[\u00b7|]\s*", ", ", str(role["tech"]))
            height += 1 + len(self._wrap("Keywords: " + terms,
                                         300.5, size=7.2)) * 8.9 + 1
        return height + (25 if with_divider else 0)

    @staticmethod
    def _compact_dates(value):
        months = ("January", "February", "March", "April", "May", "June",
                  "July", "August", "September", "October", "November", "December")
        numbers = {month.lower(): f"{index:02d}" for index, month in enumerate(months, 1)}
        pattern = r"\b(" + "|".join(months) + r")\s+(\d{4})\b"
        return re.sub(pattern,
                      lambda match: f"{numbers[match.group(1).lower()]}/{match.group(2)}",
                      str(value or "").replace("\u2013", "-"), flags=re.I)

    def _engagement_heading_lines(self, engagement):
        name = str(engagement.get("name") or "")
        return self._wrap(name, self.MAIN_W - 25.5, "Rubik", "B", 8.2)

    def _engagement_header(self, engagement, y, *, continued=False):
        if continued:
            lines = self._wrap(str(engagement.get("name") or "") + " (continued)",
                               self.MAIN_W - 25.5, "Rubik", "B", 8.2)
        else:
            lines = self._engagement_heading_lines(engagement)
        for index, line in enumerate(lines):
            self._text(line, self.MAIN_X + 25.5, y + index * 10.2,
                       family="Rubik", style="B", size=8.2, color=COMPANY_BLUE)
        return y + max(1, len(lines)) * 10.2 + 2

    def _engagement_height(self, engagement, *, with_divider=False):
        height = max(1, len(self._engagement_heading_lines(engagement))) * 10.2 + 2
        for bullet in engagement.get("bullets") or []:
            height += len(self._wrap(bullet, 300.5, size=7.7)) * 9.4 + 1.4
        if engagement.get("tech"):
            terms = re.sub(r"\s*[\u00b7|]\s*", ", ", str(engagement["tech"]))
            height += 2 + len(self._wrap("Keywords: " + terms, 300.5, size=7.2)) * 8.9
        return height + (20 if with_divider else 0)

    def _group_header_height(self, role):
        company = str(role.get("company") or "")
        title_lines = len(self._wrap(role.get("title") or "", 312,
                                     "Rubik", "", 9.1))
        company_lines = len(self._wrap(company, 312, "Rubik", "B", 8.2))
        height = 27.4 + max(0, title_lines - 1) * 10.5 + company_lines * 10.2
        progression = role.get("progression")
        if progression:
            height += len(self._wrap(progression, self.MAIN_W - 25.5,
                                     size=7.7)) * 9.5 + 5
        return height

    def _group_continuation(self, role):
        y = self._new_experience_page()
        self._text(f"{role.get('company', '')} (continued)",
                   self.MAIN_X + 25.5, y, family="Rubik", style="B",
                   size=8.2, color=COMPANY_BLUE)
        return y + 15

    def _render_grouped_role(self, role, y, *, with_divider):
        engagements = role.get("engagements") or []
        if not engagements:
            return y
        first = self._engagement_height(engagements[0])
        header = self._group_header_height(role)
        fresh_capacity = self.BOTTOM - (self.PAGE_TOP + 22.5)
        if y + header + min(first, 45) > self.BOTTOM and header + first <= fresh_capacity:
            y = self._new_experience_page()
        elif y + header + 35 > self.BOTTOM:
            y = self._new_experience_page()
        y = self._role_header({**role, "dates": self._compact_dates(role.get("dates"))}, y)
        progression = role.get("progression")
        if progression:
            y = self._lines(progression, self.MAIN_X + 25.5, y,
                            self.MAIN_W - 25.5, size=7.7, leading=9.5) + 5
        for index, engagement in enumerate(engagements):
            divided = index < len(engagements) - 1
            height = self._engagement_height(engagement)
            if y + height > self.BOTTOM and height + 15 <= fresh_capacity:
                y = self._group_continuation(role)
            elif y + 45 > self.BOTTOM:
                y = self._group_continuation(role)
            y = self._engagement_header(engagement, y)
            bullets = [str(item) for item in engagement.get("bullets") or []
                       if str(item).strip()]
            for bullet in bullets:
                bullet_height = len(self._wrap(bullet, 300.5, size=7.7)) * 9.4 + 1.4
                if y + bullet_height > self.BOTTOM:
                    y = self._group_continuation(role)
                    y = self._engagement_header(engagement, y, continued=True)
                y = self._bullet(bullet, y)
            if engagement.get("tech"):
                terms = re.sub(r"\s*[\u00b7|]\s*", ", ", str(engagement["tech"]))
                tech_height = 2 + len(self._wrap("Keywords: " + terms,
                                                  300.5, size=7.2)) * 8.9
                if y + tech_height > self.BOTTOM:
                    y = self._group_continuation(role)
                    y = self._engagement_header(engagement, y, continued=True)
                y = self._keywords(engagement["tech"], y + 1)
            if divided:
                if y + 20 <= self.BOTTOM:
                    self._rule(self.MAIN_X + 25.5, self.MAIN_X + self.MAIN_W,
                               y + 8, dashed=True)
                    y += 20
        if with_divider:
            if y + 25 <= self.BOTTOM:
                self._rule(self.MAIN_X + 25.5, self.MAIN_X + self.MAIN_W,
                           y + 8, dashed=True)
                y += 25
        return y

    def _render_experience(self, experience, y):
        experience = list(experience)
        maiborn_index = next((index for index, role in enumerate(experience)
                              if str(role.get("company") or "").casefold().startswith("maibornwolff")), None)
        cto_index = next((index for index, role in enumerate(experience)
                          if re.search(r"\b(?:cto|chief technology officer)\b",
                                       str(role.get("title") or ""), re.I)), None)
        if maiborn_index is not None and cto_index is not None and maiborn_index > cto_index:
            experience.insert(cto_index, experience.pop(maiborn_index))
        for index, role in enumerate(experience):
            if role.get("engagements"):
                y = self._render_grouped_role(role, y,
                                               with_divider=index < len(experience) - 1)
                continue
            bullets = [str(item) for item in role.get("bullets") or [] if str(item).strip()]
            full_height = self._role_height(role, with_divider=index < len(experience) - 1)
            if y + full_height > self.BOTTOM and full_height <= self.BOTTOM - (self.PAGE_TOP + 22.5):
                y = self._new_experience_page()
            elif y + 45 > self.BOTTOM:
                y = self._new_experience_page()
            y = self._role_header(role, y)
            for bullet_index, bullet in enumerate(bullets):
                height = len(self._wrap(bullet, 300.5, size=7.7)) * 9.4 + 1.4
                tech = role.get("tech") if bullet_index == len(bullets) - 1 else None
                tech_height = (len(self._wrap("Keywords: " + str(tech), 300.5,
                                              size=7.2)) * 8.9 + 2 if tech else 0)
                if y + height + tech_height > self.BOTTOM:
                    y = self._new_experience_page()
                    self._text(f"{role.get('title', '')} (continued)", self.MAIN_X + 25.5,
                               y, family="Rubik", size=8.8, color=GRAY)
                    y += 14
                y = self._bullet(bullet, y)
            if role.get("tech"):
                tech_height = len(self._wrap("Keywords: " + str(role["tech"]),
                                             300.5, size=7.2)) * 8.9 + 2
                if y + tech_height > self.BOTTOM:
                    y = self._new_experience_page()
                    self._text(f"{role.get('title', '')} (continued)", self.MAIN_X + 25.5,
                               y, family="Rubik", size=8.8, color=GRAY)
                    y += 14
                y = self._keywords(role["tech"], y + 1)
            if index < len(experience) - 1:
                self._rule(self.MAIN_X + 25.5, self.MAIN_X + self.MAIN_W,
                           y + 8, dashed=True)
                y += 25
        return y

    def render(self):
        self._plan_sidebar()
        self.add_page()
        summary = self.profile.get("summary") or ""
        heading_y = 93.5
        self._section("Summary", self.MAIN_X, self.MAIN_X + self.MAIN_W,
                      heading_y)
        y = self._lines(summary, self.MAIN_X, heading_y + 18, self.MAIN_W,
                        size=7.6, leading=9.5)
        y += 8
        self._section("Experience", self.MAIN_X, self.MAIN_X + self.MAIN_W, y)
        y += 22.5
        y = self._render_experience(self.profile.get("experience") or [], y)
        # A short experience column can leave room for Languages and Education
        # while their reserved sidebar page would otherwise be mostly empty.
        # Decide only after rendering experience, using its actual end position.
        if self.page_no() == 1 and self._second_page_only_has_personal_details():
            commands, end_y = self._main_personal_details(y + 14)
            if end_y > self.BOTTOM:
                commands, end_y = self._compact_main_personal_details(y + 14)
            if end_y <= self.BOTTOM:
                for command, x, width in commands:
                    self._draw_side_command(command, x=x, width=width)
                self._side_schedule.pop(2)
                self._sidebar_page_count = 1
            else:
                education, languages, end_y = self._split_personal_details_across_columns(y + 14)
                if end_y <= self.BOTTOM:
                    for command in education:
                        self._draw_side_command(command, x=self.MAIN_X, width=self.MAIN_W)
                    for command in languages:
                        self._draw_side_command(command)
                    self._side_schedule.pop(2)
                    self._sidebar_page_count = 1
        while self.page_no() < self._sidebar_page_count:
            self.add_page()
