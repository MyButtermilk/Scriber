from scriber_polishing.sst_parser import parse_sst
from scriber_polishing.target_renderer import render_html, render_html_clipboard, render_markdown, render_plain_text


def test_target_renderers_project_the_document_without_sst_markup() -> None:
    document = parse_sst(
        "[DOC]\n[SUBJECT]Status & Planung[/SUBJECT]\n[P]Bitte [B]heute[/B] antworten.[/P]\n"
        "[UL]\n[LI1]Erster Punkt[/LI1]\n[LI2]Unterpunkt[/LI2]\n[/UL]\n[/DOC]"
    )

    assert render_plain_text(document) == "Status & Planung\n\nBitte heute antworten.\n\n- Erster Punkt\n  - Unterpunkt"
    assert (
        render_markdown(document)
        == "__**Status & Planung**__\n\nBitte **heute** antworten.\n\n- Erster Punkt\n  - Unterpunkt"
    )
    assert render_html(document) == (
        "<p><strong><u>Status &amp; Planung</u></strong></p><p>Bitte <strong>heute</strong> antworten.</p>"
        "<ul><li>Erster Punkt<ul><li>Unterpunkt</li></ul></li></ul>"
    )


def test_html_renderer_escapes_model_text_and_clipboard_contains_valid_offsets() -> None:
    document = parse_sst("[DOC]\n[P]&lt;script&gt;alert(1)&lt;/script&gt; &amp; [U]sicher[/U][/P]\n[/DOC]")

    html = render_html(document)
    clipboard = render_html_clipboard(document)

    assert html == "<p>&amp;lt;script&amp;gt;alert(1)&amp;lt;/script&amp;gt; &amp;amp; <u>sicher</u></p>"
    assert "<script>" not in html
    start_html = int(clipboard.split("\r\n")[1].split(":")[1])
    end_html = int(clipboard.split("\r\n")[2].split(":")[1])
    assert clipboard.encode("utf-8")[start_html:end_html].decode("utf-8").startswith("<html>")


def test_html_renderer_uses_nonbreaking_spaces_for_quantities_and_legal_references_only() -> None:
    document = parse_sst(
        "[DOC]\n"
        "[P]Der Betrag beträgt 2.500,00 € bei 19 %, 34 €/m² und 0,19 €/kWh.[/P]\n"
        "[P]§ 8 Absatz 1 Satz 2 EStG gilt für 91 kWh/(m²·a) um 10 Uhr.[/P]\n"
        "[/DOC]"
    )

    assert render_plain_text(document) == (
        "Der Betrag beträgt 2.500,00 € bei 19 %, 34 €/m² und 0,19 €/kWh.\n\n"
        "§ 8 Absatz 1 Satz 2 EStG gilt für 91 kWh/(m²·a) um 10 Uhr."
    )
    assert render_markdown(document) == (
        "Der Betrag beträgt 2.500,00 € bei 19 %, 34 €/m² und 0,19 €/kWh.\n\n"
        "§ 8 Absatz 1 Satz 2 EStG gilt für 91 kWh/(m²·a) um 10 Uhr."
    )
    assert render_html(document) == (
        "<p>Der Betrag beträgt 2.500,00&nbsp;€ bei 19&nbsp;%, 34&nbsp;€/m² "
        "und 0,19&nbsp;€/kWh.</p>"
        "<p>§&nbsp;8 Absatz&nbsp;1 Satz&nbsp;2 EStG gilt für "
        "91&nbsp;kWh/(m²·a) um 10&nbsp;Uhr.</p>"
    )


def test_html_renderer_covers_the_declared_closed_unit_vocabulary() -> None:
    document = parse_sst(
        "[DOC]\n"
        "[P]Gemessen wurden 7 l/(m²·a), 12 dB(A), 9 kWp und 4 vCPU für € 50.[/P]\n"
        "[P]Das Paket umfasst 32 MiB, 2 TB und 18 kg.[/P]\n"
        "[/DOC]"
    )

    assert render_plain_text(document) == (
        "Gemessen wurden 7 l/(m²·a), 12 dB(A), 9 kWp und 4 vCPU für € 50.\n\n"
        "Das Paket umfasst 32 MiB, 2 TB und 18 kg."
    )
    assert render_html(document) == (
        "<p>Gemessen wurden 7&nbsp;l/(m²·a), 12&nbsp;dB(A), "
        "9&nbsp;kWp und 4&nbsp;vCPU für €&nbsp;50.</p>"
        "<p>Das Paket umfasst 32&nbsp;MiB, 2&nbsp;TB und 18&nbsp;kg.</p>"
    )
