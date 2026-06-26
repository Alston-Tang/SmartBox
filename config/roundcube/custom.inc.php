<?php
/**
 * SmartBox Roundcube overrides.
 *
 * The official image only includes this file from /var/roundcube/config. The
 * SMTP user/helo settings are generated at container start into
 * /var/roundcube/smtp.generated.inc.php (see services/roundcube/10-smtp-config.sh)
 * and pulled in below.
 */

$config['product_name'] = 'SmartBox Webmail';

@include('/var/roundcube/smtp.generated.inc.php');
