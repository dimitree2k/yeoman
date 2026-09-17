# Gateway-Testbereinigung — Entscheidungsnotiz

## Umfang

Diese Änderung reduziert nachweislich redundante Gateway-Tests. Es wurde kein Produktionscode geändert und keine Assertion abgeschwächt.

Geändert wurden auf `main`:

- `tests/gateway/test_responder_memory_recall.py`: drei Social-Holdback-Duplikate entfernt.
- `tests/gateway/test_processing_routing_criteria.py`: zwei bereits stärker abgedeckte Routing-Fälle und ein ungenutzter Helper entfernt.

Die verbleibenden Nachweise liegen in `test_responder_social_holdback.py` und `test_processing_threads.py`. Security-/Privacy-Grenzen, Übergangsarchitektur und spröde Participation-Kompositionsprüfungen wurden nicht entfernt, weil dafür entweder ein stärkerer Ersatztest oder eine explizite fachliche Retirement-Entscheidung fehlt.

## Evidenz

Im isolierten Vergleichsstand sank die Gateway-Collection von 1849 auf 1844 Fällen und der Testquellumfang von 52650 auf 52469 Zeilen; die Testdateianzahl blieb bei 124. Die betroffenen Tests bestanden nach der Bereinigung mit `56 passed`; Ruff und `git diff --check` waren sauber.

Nach der Übernahme in den bestehenden `main`-Checkout bestanden die betroffenen Tests mit `56 passed in 11.25s`. Die vollständige Gateway-Suite bestand mit `1866 passed, 1 skipped in 125.46s`. Die höhere Fallzahl gegenüber dem isolierten Vergleich stammt aus bereits vorhandenen uncommitteten Main-Änderungen.

Es gab keinen Deploy, Neustart, Live-Test, Commit oder Push. Runtime- und Transportzustellung sind durch diese lokalen Tests nicht belegt.
