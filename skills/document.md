# Document / letter

Use this checklist for letters, reports, proposals, essays, resumes, memos and
other long-form documents.

## Brief and acceptance criteria
- [ ] Restate the reader, purpose, desired action, length, tone, format,
      deadline and confidentiality level.
- [ ] Turn the brief into numbered acceptance criteria (`R1...Rn`) with a
      measurable pass/fail test and an evidence file for each.
- [ ] List the facts, names, dates, numbers, sources and attachments the
      document must contain; record unknowns as assumptions to confirm.
- [ ] Outline the document before drafting. Give every section a job and
      remove sections that do not serve the brief.

## Letter anatomy
- [ ] For a letter, include the sender block, date, recipient name, title,
      organisation and address, reference or subject line, salutation, body,
      sign-off, signature block, and enclosures/cc where needed.
- [ ] Verify the recipient's name, title, organisation, address order and
      salutation; never guess an honorific or spelling.
- [ ] Use one date format throughout and make it unambiguous. Include the year
      where the letter may be filed or read later.
- [ ] Keep a routine letter to one page unless the brief asks for more. State
      the purpose in the first paragraph and the requested action in the last.
- [ ] The opening and closing must work standalone for a reader who skims only
      those two paragraphs.

## Resume and cover letter specifics
- [ ] For a resume, use the requested format (reverse-chronological or
      skills-based), consistent dates, quantified achievements and relevant
      keywords from the role; keep to the requested page limit.
- [ ] Do not include a photo, date of birth, marital status or other protected
      personal data unless the brief or local convention requires it; keep
      contact details minimal.
- [ ] Use an ATS-safe structure: standard headings, no critical text inside
      tables, text boxes or images, and a selectable PDF plus editable source.
- [ ] A cover letter names the role and organisation, shows fit with specific
      evidence, and does not merely repeat the resume.

## Structure and argument
- [ ] Long documents have a title page, table of contents, executive summary
      where useful, numbered headings, introduction, body, conclusion,
      appendices and a reference list.
- [ ] Each paragraph has one idea and a clear topic sentence; use signposting
      and short paragraphs rather than a wall of text.
- [ ] The most important point comes first. The conclusion answers the brief,
      not just the preceding section.
- [ ] Headings are descriptive and parallel; heading levels are not skipped.

## Completeness and accuracy
- [ ] Every fact, name, date, number and requirement from the brief is present
      and correct.
- [ ] No invented facts, sources, credentials or quotations. Every factual
      claim that needs support has a verifiable citation.
- [ ] Each citation resolves to a real source and follows one style
      consistently; quotations are exact and page numbers are given where
      required.
- [ ] Dates and numbers are internally consistent, units are stated, and
      derived figures recalculate correctly.
- [ ] Placeholders are visibly marked (for example `[TBC]`) and listed for the
      reader; they are never presented as finished content.

## Language and tone
- [ ] Match the requested register (formal, friendly, technical, persuasive)
      and the relationship with the reader.
- [ ] Prefer active voice, concrete verbs and plain language; define jargon on
      first use and cut filler, cliches and repetition.
- [ ] Use inclusive, neutral language and avoid unnecessary personal detail.
- [ ] Check spelling and grammar; use one spelling convention and one set of
      terms throughout.
- [ ] Count words or pages against the requested limit; remove padding rather
      than shrinking type to fit.

## Format and accessibility
- [ ] Body text 10-12 pt, line height 1.15-1.5, margins 20-25 mm, 50-75
      characters per line, left-aligned rather than justified.
- [ ] Consistent heading hierarchy, spacing, lists, tables and captions; no
      widows, orphans or headings stranded at the foot of a page.
- [ ] Page numbers, running headers, table of contents and cross-references are
      correct after the final pagination.
- [ ] Export a tagged PDF with alt text for figures, bookmarks, logical reading
      order, document language and title metadata, embedded fonts, selectable
      text and sufficient contrast.
- [ ] Remove author, company and revision metadata that the brief does not
      require; resolve all tracked changes and comments before delivery.

## Delivery
- [ ] Supply the editable source (`.docx` or Markdown), a rendered PDF and any
      requested plain-text version; all open cleanly and the text is
      selectable.
- [ ] File names are descriptive and versioned; no personal data appears in
      the file name or document properties.
- [ ] Save the requirements checklist, source notes and rendered pages as
      evidence alongside the deliverable.

## Review and quality gate
- [ ] Compare at least two independent outlines or drafts, and record why the
      chosen structure and wording are stronger.
- [ ] Use a reviewer that did not write the document. It reads the rendered
      pages (page images via `read_image` where useful), checks every numbered
      requirement, counts words/pages, checks citations and tone, and scores
      the result 0-10 against this checklist.
- [ ] Blocking defects include a missing requirement or fact, a wrong name,
      date or number, a fabricated or unresolved source, a broken layout or
      pagination, an inaccessible PDF, and leaked personal metadata.
- [ ] Fix every blocking defect; revise until the score is at least 8.5 with
      zero blocking defects, or three rounds have been used. If the quality
      loop is disabled, run the same checks yourself and record that
      independence was not possible.
- [ ] When the quality loop is enabled, save `QUALITY_REPORT.md` with the
      numbered checklist, per-round scores, defects found and fixed, evidence
      paths and the final score; otherwise include the same checklist, score
      and evidence in the final answer. In both cases end the answer with
      `Final quality score: X/10`.

## Lessons learned
<!-- Add only verified, reusable lessons as symptom -> cause -> check to add.
     Keep them short and free of names, emails, tokens and personal paths. -->
