-- "E-mail" as an origin: leads the team adds in the panel after the person
-- left their contact by e-mail. Safe to repeat.
ALTER TABLE contacts DROP CONSTRAINT IF EXISTS contacts_origin_check;
ALTER TABLE contacts ADD CONSTRAINT contacts_origin_check
  CHECK (origin IN ('anuncio_instagram', 'anuncio_facebook', 'site', 'google',
                    'indicacao', 'email', 'outro'));
