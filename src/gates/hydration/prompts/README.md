# Proposer prompts

| File | What it is |
| --- | --- |
| `propose_v1.md` … `propose_v3.md` | **Exemplars.** Working prompts written for the synthetic insurance glossary and data in this repo. They show what a tuned prompt looks like and are what the eval baselines were measured with. They are not anyone's production configuration. |
| `templates/propose_template.md` | **Template.** The same structure with `<<FILL: …>>` markers where your own domain wording goes. |

To make your own:

1. Copy `templates/propose_template.md` to `propose_v<next>.md` in this folder (for example `propose_v4.md`).
2. Replace every `<<FILL: …>>` marker. Keep the `## SYSTEM` / `## USER` headings, the `{{table}}`,
   `{{table_columns}}` and `{{columns_json}}` placeholders and the JSON reply format exactly as they are:
   the code depends on them.
3. Select it with the `prompt_version` bundle variable (or `--params prompt_version=propose_v4` on `hydrate`).
4. Measure it with `eval_hydration` against your golden set before using it.

Never edit a prompt in place once it has been used: add a new version, so eval results stay comparable.
The abbreviation dictionary works the same way: `../abbreviations.yaml` is a synthetic exemplar,
`templates/abbreviations.template.yaml` is the template.
