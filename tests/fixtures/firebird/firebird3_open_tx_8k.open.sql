UPDATE T SET TXT = 'zwei NEU (nicht abgeschlossen)', AMOUNT = 222.00 WHERE ID = 2;
DELETE FROM T WHERE ID = 3;
UPDATE T SET TXT = 'vier NEU deutlich laengerer Text als vorher, nicht abgeschlossen' WHERE ID = 4;
INSERT INTO T VALUES (6, 'sechs neu (nicht abgeschlossen)', 6.00);
SELECT COUNT(*) FROM T;
