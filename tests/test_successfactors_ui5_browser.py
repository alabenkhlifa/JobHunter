"""Offline Denodo UI5 identity, shadow control and approval regressions."""
import json
import os
from pathlib import Path
import subprocess
from unittest.mock import Mock

import pytest

from jobhunter_auto_apply.engine import inspect_page, AutoApplyEngine, ApplyConfig
from jobhunter_auto_apply.cdp import CDPError
from jobhunter_auto_apply.shadow import element_expression
from test_iframe_cdp_browser import browser  # Reuse isolated Chromium/CDP fixture.

pytestmark = pytest.mark.skipif(os.getenv("JOBHUNTER_BROWSER_TESTS") != "1",
                                reason="requires offline Chromium")

URL = "https://career55.sapsf.eu/career?company=denodosl&career_job_req_id=2521"
HTML = """<!doctype html><title>Career Opportunities: Product Architect</title>
<style>[hidden]{display:none!important}[ui5-title],ui5-wizard-xweb-dynamic-content{display:block}form{display:block;width:800px}
ui5-file-uploader-xweb-dynamic-content{display:block;width:300px;height:40px}</style>
<a href="https://www.denodo.com/">Denodo</a>
<form id="careerform"><input id="career_company" type="hidden" value="denodosl">
<input id="career_job_req_id" type="hidden" value="2521">
<ui5-title-xweb-candidate-experience ui5-title>Product Architect (2521)</ui5-title-xweb-candidate-experience>
<ui5-wizard-xweb-dynamic-content>
<ui5-file-uploader-xweb-dynamic-content accessible-name="Resume / CV"></ui5-file-uploader-xweb-dynamic-content>
</ui5-wizard-xweb-dynamic-content>
<ui5-button-xweb-dynamic-content>Next</ui5-button-xweb-dynamic-content>
</form><script>
customElements.define('ui5-title-xweb-candidate-experience', class extends HTMLElement {
 connectedCallback(){this.attachShadow({mode:'open'}).innerHTML='<h2><slot></slot></h2>';}
});
customElements.define('ui5-wizard-xweb-dynamic-content', class extends HTMLElement {
 connectedCallback(){this.attachShadow({mode:'open'}).innerHTML=
  '<ui5-wizard-tab-xweb-dynamic-content selected title-text="Getting Started *"></ui5-wizard-tab-xweb-dynamic-content><slot></slot>';}
});
customElements.define('ui5-file-uploader-xweb-dynamic-content', class extends HTMLElement {
 connectedCallback(){this.attachShadow({mode:'open'}).innerHTML='<input type="file" accept=".pdf,.txt">';}
});
customElements.define('ui5-button-xweb-dynamic-content', class extends HTMLElement {
 connectedCallback(){this.attachShadow({mode:'open'}).innerHTML='<button><slot></slot></button>';
  this.addEventListener('click',()=>window.clicked=true);}
 get disabled(){return this.hasAttribute('disabled');}
});
</script>"""


def inspect_html(html=HTML, url=URL):
    def evaluate(expression):
        result = subprocess.run(
            ["node", str(Path(__file__).with_name("browser_application_inspection.cjs"))],
            input=json.dumps({"html": html, "url": url, "expression": expression}),
            text=True, capture_output=True, timeout=30)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)
    client = Mock()
    client.evaluate.side_effect = evaluate
    return inspect_page(client)


def test_ui5_binds_employer_requisition_and_visible_resume_control():
    page = inspect_html()
    assert page.verifies_application_for("Product Architect", "Denodo")
    assert not page.verifies_application_for("Product Architect", "Other Employer")
    assert not page.verifies_application_for("Junior Product Architect", "Denodo")
    assert page.missing_required == ["Resume / CV"]
    file = next(x for x in page.inputs if x["type"] == "file")
    assert ">>>" in file["selector"] and file["form"] == "careerform"
    assert page.application_context["step"] == "Getting Started *"


@pytest.mark.parametrize("change", [
    "wrong_host", "wrong_tenant", "wrong_query", "duplicate_query", "wrong_req",
    "wrong_heading", "duplicate_heading", "conflicting_req", "no_brand", "hidden_brand",
    "hidden_form", "hidden_wizard", "second_wizard", "missing_next", "foreign_req",
])
def test_ui5_identity_and_control_failures_are_closed(change):
    html, url = HTML, URL
    if change == "wrong_host": url = url.replace("career55.sapsf.eu", "example.test")
    if change == "wrong_tenant": html = html.replace('value="denodosl"', 'value="other"')
    if change == "wrong_query": url = url.replace("2521", "9999")
    if change == "duplicate_query": url += "&company=denodosl"
    if change == "wrong_req": html = html.replace('value="2521"', 'value="9999"')
    if change == "wrong_heading": html = html.replace("Architect (2521)", "Architect (9999)")
    if change == "duplicate_heading":
        html = html.replace("<ui5-wizard-", "<ui5-title-xweb-candidate-experience>Product Architect (2521)</ui5-title-xweb-candidate-experience><ui5-wizard-", 1)
    if change == "conflicting_req":
        html = html.replace("</form>", '<input id="career_job_req_id" type="hidden" value="9999"></form>')
    if change == "no_brand": html = html.replace("www.denodo.com", "example.test")
    if change == "hidden_brand": html = html.replace("<a href", "<a hidden href")
    if change == "hidden_form": html = html.replace('<form id=', '<form hidden id=')
    if change == "hidden_wizard": html = html.replace("<ui5-wizard-xweb-dynamic-content>", "<ui5-wizard-xweb-dynamic-content style='display:none'>")
    if change == "second_wizard": html = html.replace("</form>", "<ui5-wizard-xweb-dynamic-content></ui5-wizard-xweb-dynamic-content></form>")
    if change == "missing_next": html = html.replace(">Next</ui5-button", ">Dismiss</ui5-button")
    if change == "foreign_req": html = html.replace('<input id="career_job_req_id" type="hidden" value="2521">', "") + '<input id="career_job_req_id" type="hidden" value="2521">'
    assert not inspect_html(html, url).verifies_application_for("Product Architect", "Denodo")


def test_ui5_matching_requisition_copies_and_hidden_background_fields():
    html = HTML.replace("</form>", '<input id="career_job_req_id" type="hidden" value="2521">'
                        '<input type="email" aria-label="Unrelated hidden field" required hidden></form>')
    page = inspect_html(html)
    assert page.verifies_application_for("Product Architect", "Denodo")
    assert page.missing_required == ["Resume / CV"]


def test_authenticated_portal_binds_hidden_tenant_and_requisition_without_query_identifiers():
    page = inspect_html(url="https://career55.sapsf.eu/portalcareer")
    assert page.verifies_application_for("Product Architect", "Denodo")
    assert not inspect_html(url="https://career55.sapsf.eu/portalcareer?company=other").verifies_application_for(
        "Product Architect", "Denodo")


def test_ui5_shadow_required_and_invalid_fields_are_not_hidden_by_component_hosts():
    html = HTML.replace("</form>", """<ui5-input-xweb-dynamic-content accessible-name="Phone"
        required value-state="Error"></ui5-input-xweb-dynamic-content></form>""") + """<script>
        customElements.define('ui5-input-xweb-dynamic-content',class extends HTMLElement{
          connectedCallback(){this.attachShadow({mode:'open'}).innerHTML='<input type="tel" required>';}
        });</script>"""
    page = inspect_html(html)
    assert page.verifies_application_for("Product Architect", "Denodo")
    assert "Phone" in page.missing_required
    assert page.invalid_fields == ["Phone"]
    assert not page.safe_to_continue


@pytest.mark.parametrize("change", ["complete", "failed", "hidden", "outside_collection"])
def test_ui5_stored_resume_survives_a_cleared_native_file_picker(change):
    item = ('<span ui5-upload-collection-item upload-state="Complete" '
            'file-name="Synthetic.pdf">Stored resume</span>')
    if change == "failed": item = item.replace('upload-state="Complete"', 'upload-state="Error"')
    if change == "hidden": item = item.replace("<span ", "<span hidden ")
    start = '<ui5-file-uploader-xweb-dynamic-content accessible-name="Resume / CV">'
    end = "</ui5-file-uploader-xweb-dynamic-content>"
    html = HTML.replace(start, '<div ui5-upload-collection>' + start).replace(end, end + item + "</div>")
    if change == "outside_collection":
        html = html.replace(item, "").replace("</form>", item + "</form>")
    page = inspect_html(html)
    file = next(f for f in page.inputs if f["type"] == "file")
    assert file["value_present"] is (change == "complete")
    assert file["stored_files"] == (["Synthetic.pdf"] if change == "complete" else [])


@pytest.mark.parametrize("required,checked,hidden", [(True,False,False), (True,True,False),
                                                    (False,False,False), (True,False,True)])
def test_ui5_checkbox_without_a_native_input_preserves_required_and_checked_state(required, checked, hidden):
    attrs = " ".join(x for x, enabled in [("required",required),("checked",checked),("hidden",hidden)] if enabled)
    html = HTML.replace("</form>", '<ui5-checkbox-xweb-dynamic-content text="Consent" ' + attrs +
                        ' style="display:block"></ui5-checkbox-xweb-dynamic-content></form>') + """<script>
        customElements.define('ui5-checkbox-xweb-dynamic-content',class extends HTMLElement{
          connectedCallback(){this.attachShadow({mode:'open'}).innerHTML='<div role="checkbox">Consent</div>';}
          get checked(){return this.hasAttribute('checked');}
          get required(){return this.hasAttribute('required');}
        });</script>"""
    page = inspect_html(html)
    checkbox = [f for f in page.inputs if f["type"] == "checkbox"]
    assert len(checkbox) == (0 if hidden else 1)
    assert ("Consent" in page.missing_required) is (required and not checked and not hidden)

def test_shadow_upload_fill_and_trusted_click(browser, tmp_path):
    open_page, _, _ = browser
    client = open_page(HTML + """<x-profile></x-profile><script>
    customElements.define('x-profile',class extends HTMLElement{
      connectedCallback(){this.attachShadow({mode:'open'}).innerHTML='<input id="name"><button id="go">Continue</button>';
      this.shadowRoot.querySelector('button').onclick=e=>window.trusted=e.isTrusted;}
    });</script>""")
    selector = "ui5-file-uploader-xweb-dynamic-content >>> input[type=file]"
    file = tmp_path / "synthetic.txt"
    file.write_text("Synthetic fixture")
    client.upload_file(selector, str(file))
    assert client.evaluate(element_expression(selector) + ".files[0].name") == file.name
    client.fill("x-profile >>> #name", "Fixture")
    assert client.evaluate(element_expression("x-profile >>> #name") + ".value") == "Fixture"
    client.click("x-profile >>> #go")
    assert client.evaluate("window.trusted") is True
    with pytest.raises(CDPError, match="ambiguous"):
        client.upload_file("ui5-file-uploader-xweb-dynamic-content, x-profile >>> input", str(file))


def test_ui5_continuation_never_authorizes_submission():
    page = inspect_html()
    client = Mock()
    engine = AutoApplyEngine(ApplyConfig(expected_page_url=URL, evidence_enabled=False,
                                        tracker_sync=False), client=client)
    engine._refuse_resubmit = Mock()
    engine._verify_before_mutation = Mock(return_value=page)
    with pytest.raises(PermissionError):
        engine.click_submit("fixture", "anything", approved=False)
    client.click.assert_not_called()
    with pytest.raises(CDPError, match="complete SAP UI5 Review"):
        engine._submit_ui5("fixture", client, page, page.buttons[0]["selector"])
    client.click.assert_not_called()
