# OBJ-44: re-creación del trabajo de t_ee1f4e1a — revisión de 2 skills con mediciones fechadas

Fecha: 2026-09-14 · Ejecutado por: pr-nanogpt (task t_854fd824, z-ai/glm-5.3-flash) · Ítem 41 de
`~/.hermes/profiles/pr-ollama/docs/obj44-cola-viva-post-1040f604-2026-09-14.md`.

## Por qué existe esta ficha

`t_ee1f4e1a` («OBJ-44-AUTOQUEUE: revisar 2 skills aleatorias y actualizar
mediciones/pendientes fechados», creada 2026-09-12 20:31:59, pr-ollama/gpt-oss:20b)
murió 4 veces por protocol-violation (rc=0 sin llamada terminal) y quedó archivada
sin `completed_at` — pérdida REAL, la única viva de OBJ-44 tras el estampado de
t_bbd6844e. El guard temporal de `abandon-superseded.py` no la cubre: su único
candidato por título (t_8aca6e5b) es ANTERIOR (18:57 vs 20:31).

Dos vías sancionadas por el ítem 41: re-crear el trabajo, o estampar con
justificación manual. Se hizo la (a), y la estampa manual de cierre apunta aquí.

Nota de máquina: existía un gemelo posterior ya done, **t_2d851bf3** (creado
2026-09-12 22:15, done 22:17, tocó hermes-agent y hermes-ollama-quota del perfil).
El script de estampas no lo encontró porque el match es por título normalizado y
el título del gemelo interpola «skills —». El trabajo de esta ficha es una
re-ejecución fresca del seed (no un duplicado del gemelo: 2 días de deriva nueva).

## Skills revisadas (evidencia sha256)

### 1. hermes-quota-aware-dispatch (perfil pr-ollama, autonomous-ai-agents)

`~/.hermes/profiles/pr-ollama/skills/autonomous-ai-agents/hermes-quota-aware-dispatch/SKILL.md`
sha256 tras edición: `da5066a39325d4de270f269be122b2238634fe2547d27a0ea217e1637bdfa0d5` (61849 B)

- Tabla de perfiles: `pr-nanogpt` worker/interactive `zai-org/glm-5.2` →
  **z-ai/glm-5.3-flash** (verificado contra `~/.hermes/profiles/pr-nanogpt/config.yaml`
  `model.default: z-ai/glm-5.3-flash`; cobertura por suscripción viva — esta sesión
  corre sobre ella; la nota qwen3.5-4b/402 y MULTI-PROV-10 se conservan).
- Tabla de perfiles: `pr-opencode` worker qwen3.8-flash anotado con ventana
  mensual ~74% (medido 2026-09-14, ver fuentes).
- Pitfall 11: «healthiest cheap profile» re-fechado (Sep 14 2026: qwen3.8-flash
  @ opencode-go rolling 7% / weekly 9%, mensual ~74%; ollama-cloud weekly 2.8%).
- Pitfall 8: «Keys expire (current: 2026-09-10)» → la clave de Sep 2026 murió el
  2026-09-10 y NO se reemplazó (`openrouter-quota` falla sin `data`;
  last-good congelado en 2026-09-09). Tratar clave muerta como perfil no disponible.

### 2. hermes-ollama-quota (perfil pr-ollama, autonomous-ai-agents)

`~/.hermes/profiles/pr-ollama/skills/autonomous-ai-agents/hermes-ollama-quota/SKILL.md`
sha256 tras edición: `f146c0632b87e5163b3014e36ac933fe38d7001a6941393f6ae639ad5c42ef24` (19902 B)

- §Key Rotation: «expires 2026-09-10» (futuro) → estado real: expirada y sin
  reemplazar; rotación pendiente de acción del usuario (3 pasos conservados).
- Tabla NanoGPT: `allowOverage` «False, not enforced» → el campo ahora reporta
  **true** (Sep 14 2026); ninguno de los dos valores es un límite duro.
- §4 caso 101%: re-fechado con lectura viva del 2026-09-14: weekly 100.03%
  (60.02M/60M tokens, reset 2026-09-21); sustituido el reset fenecido 2026-08-17.
- Pitfall 1: «allowOverage: false is NOT enforced» → «NOT a hard limit either
  way» (false en Ago sirvió al 101.3%; true en Sep sigue sirviendo al 100.03%).

Sin cambios fabricados: toda cifra nueva proviene de las fuentes listadas; los
hechos históricos (Ago 2026, commits, precios) se preservan.

## Fuentes de las mediciones (2026-09-14)

- `ollama-quota` (consola): Sesión 1.0%, **Semanal 2.8%** (43 req / 230 req).
- `nanogpt-quota` (consola): state active, weekly tokens 60,019,905/60,000,000
  = **100.03%**, reset 2026-09-21T00:00Z, `allowOverage: True`.
- `~/.hermes/{,profiles/pr-ollama/}quota-governor/opencode_go-last-good.json`
  (15:58): rolling 6-7%, weekly 9%, **monthly 74-75%** (resets 2026-09-21 /
  2026-10-06), todos `status: ok`.
- `~/.hermes/profiles/pr-nanogpt/config.yaml`: `model.default: z-ai/glm-5.3-flash`
  (provider custom → nano-gpt.com).
- `openrouter-quota`: excepción (respuesta sin `data`) + last-good 2026-09-09 con
  `expires_at 2026-09-10` → clave muerta.

## Pendiente para el usuario (no accionable por el worker)

- Rotar la clave de OpenRouter (el skill documenta los 3 pasos).
