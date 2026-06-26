<?php
/**
 * SmartBox Roundcube overrides.
 *
 * Stalwart is plaintext-only on the internal Docker network (IMAP 143, SMTP 25).
 * SMTP AUTH is required to send mail and relay to external domains.
 */

$config['smtp_user'] = '%u';
$config['smtp_pass'] = '%p';
$config['smtp_auth_type'] = 'LOGIN';

$config['product_name'] = 'SmartBox Webmail';
