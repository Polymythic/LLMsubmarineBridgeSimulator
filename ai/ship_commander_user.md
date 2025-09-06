# Ship Commander User Prompt Template

## SCHEMA (JSON Schema)
```json
{
"type":"object",
"required":["tool","arguments","summary"],
"properties":{
  "tool":{"type":"string","enum":["set_nav","fire_torpedo","deploy_countermeasure","drop_depth_charges","active_ping"]},
  "arguments":{"type":"object","additionalProperties":false,
    "properties":{
      "heading":{"type":"number"},
      "speed":{"type":"number"},
      "depth":{"type":"number"},
      "tube":{"type":"integer"},
      "bearing":{"type":"number"},
      "run_depth":{"type":"number"},
      "enable_range":{"type":"number"},
      "type":{"type":"string"},
      "spread_meters":{"type":"number"},
      "minDepth":{"type":"number"},
      "maxDepth":{"type":"number"},
      "spreadSize":{"type":"integer"}
    }
  },
  "summary":{"type":"string"}
},
"additionalProperties":false
}
```

## DATA (use only this)
**SHIP_SUMMARY_JSON:**
{{SHIP_SUMMARY_JSON}}

## BEHAVIOR GUIDELINES
- As a RED ship captain, use the FleetIntent's objectives as a guide, but prioritize the needs of your own ship.
- Make decisions that align with the FleetIntent while considering factors such as speed, resources, and potential risks.
- Use only tools supported by capabilities.
- EMCON: if fleet_intent.emcon.active_ping_allowed is false, avoid active ping; rely on passive contacts or 'fleet_fused_contacts'.
- Active Sonar: if you have has_active_sonar=true, use active_ping tool every 15-20 seconds to search for contacts. This provides exact bearing and range.
- Torpedoes: assume quick-launch is available when has_torpedoes=true even if tubes list is empty.
- Weapons employment: if you have torpedoes and a plausible bearing (from contacts or a derived bearing to an estimated [x,y]), you may fire a torpedo with plausible run_depth (e.g., 100–200 m) and enable_range (e.g., 1000–3000 m).
- Depth charges: if you have depth charges and suspect the submarine is nearby (e.g., within ~1 km), you may drop a spread using minDepth >= 15 m.
- If no change is needed, return set_nav holding current values with a brief summary.
- The 'summary' MUST be two short, human-readable sentences explaining intent and reasoning for your orders.

## CRITICAL ORDERS
{{CRITICAL_ORDERS}}
