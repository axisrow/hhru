# Portfolio evidence detection (#348)

Vacancy/card text is passed through `detect_portfolio_evidence`. The
deterministic stage requires an evidence artefact (GitHub/GitLab, portfolio,
project, demo, case study, bot, or deployed service) and request language
(attach/include/provide a link, or Russian equivalents) in the same sentence.
This avoids treating technology mentions such as **GitHub Actions** or a
company's GitHub link as a candidate request.

`required` is used for explicit or mandatory wording (`must`, `обязательно`,
`пришлите`, etc.); `preferred` is used for softer wording (`желательно`,
`будет плюсом`, `nice to have`). The matching sentence is retained in
`evidence`, and confidence is a heuristic (0.95/0.82), not a probability.

An optional second-stage classifier can return strict JSON with `level`,
`confidence`, and `rationale`. Results below 0.7, malformed responses, and
transport failures fall back to the keyword result. The model cannot turn an
incidental mention into a requirement, and the parser never writes or changes
an application. The signal is exposed on `VacancyCard` and included in the
scoring prompt for downstream ranking/application preparation.
