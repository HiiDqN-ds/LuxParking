<?php
/**
 * Plugin Name: ParkPilot Booking
 * Description: Adds a parking price check and booking box to any page with the [parkpilot] shortcode.
 * Version: 1.0.0
 * Requires PHP: 7.4
 * License: GPL-2.0-or-later
 *
 * Settings: Settings > ParkPilot (host, public key, lot id, tenant slug, lot slug, colour).
 * Usage:    [parkpilot]  or  [parkpilot lang="de" color="#14385C" lot="2" lot_slug="other-lot"]
 *
 * Only your PUBLIC key (pk_...) belongs here. Never put a secret key (sk_...) into WordPress.
 */
if (!defined('ABSPATH')) {
    exit;
}

const PARKPILOT_OPTION = 'parkpilot_booking_settings';

function parkpilot_defaults() {
    return array('host' => '', 'key' => '', 'lot' => '', 'tenant' => '', 'lot_slug' => '', 'color' => '#14385C', 'lang' => '');
}

function parkpilot_settings() {
    return wp_parse_args(get_option(PARKPILOT_OPTION, array()), parkpilot_defaults());
}

/* ---- settings page -------------------------------------------------------------------------- */
add_action('admin_menu', function () {
    add_options_page('ParkPilot', 'ParkPilot', 'manage_options', 'parkpilot-booking', 'parkpilot_render_settings');
});

add_action('admin_init', function () {
    register_setting('parkpilot_group', PARKPILOT_OPTION, array('sanitize_callback' => 'parkpilot_sanitize'));
});

function parkpilot_sanitize($input) {
    $out = parkpilot_defaults();
    $host = isset($input['host']) ? esc_url_raw(trim($input['host']), array('https')) : '';
    $out['host'] = rtrim($host, '/');
    $key = isset($input['key']) ? trim($input['key']) : '';
    // Refuse secret keys outright: a pk_ key is safe in a web page, an sk_ key is not.
    $out['key'] = (strpos($key, 'pk_') === 0 && preg_match('/^pk_[A-Za-z0-9_.\-]+$/', $key)) ? $key : '';
    if ($key !== '' && $out['key'] === '') {
        add_settings_error(PARKPILOT_OPTION, 'parkpilot_key', 'Only a public key starting with pk_ can be used.');
    }
    $out['lot'] = isset($input['lot']) ? preg_replace('/\D/', '', $input['lot']) : '';
    $out['tenant'] = isset($input['tenant']) ? sanitize_title($input['tenant']) : '';
    $out['lot_slug'] = isset($input['lot_slug']) ? sanitize_title($input['lot_slug']) : '';
    $out['color'] = (isset($input['color']) && preg_match('/^#[0-9a-fA-F]{6}$/', $input['color'])) ? $input['color'] : '#14385C';
    $out['lang'] = (isset($input['lang']) && preg_match('/^[a-z]{2}$/', $input['lang'])) ? $input['lang'] : '';
    return $out;
}

function parkpilot_render_settings() {
    if (!current_user_can('manage_options')) {
        return;
    }
    $s = parkpilot_settings();
    $fields = array(
        'host' => array('ParkPilot address', 'https://park.example.com'),
        'key' => array('Public key (pk_...)', 'pk_xxxxxxxx.xxxxxxxx'),
        'lot' => array('Lot ID', '1'),
        'tenant' => array('Tenant slug', 'your-parking'),
        'lot_slug' => array('Lot slug', 'main-lot'),
        'color' => array('Button colour', '#14385C'),
        'lang' => array('Language (optional, e.g. de)', 'de'),
    );
    echo '<div class="wrap"><h1>ParkPilot</h1><form method="post" action="options.php">';
    settings_fields('parkpilot_group');
    echo '<table class="form-table">';
    foreach ($fields as $name => $meta) {
        printf('<tr><th><label for="pp_%1$s">%2$s</label></th><td><input class="regular-text" id="pp_%1$s" name="%3$s[%1$s]" value="%4$s" placeholder="%5$s"></td></tr>',
            esc_attr($name), esc_html($meta[0]), esc_attr(PARKPILOT_OPTION), esc_attr($s[$name]), esc_attr($meta[1]));
    }
    echo '</table>';
    submit_button();
    echo '<p>Add <code>[parkpilot]</code> to a page or post. Your site address must be listed in the key\'s allowed origins in ParkPilot.</p></form></div>';
}

/* ---- shortcode ------------------------------------------------------------------------------ */
add_shortcode('parkpilot', function ($atts) {
    $s = parkpilot_settings();
    $a = shortcode_atts(array('lot' => $s['lot'], 'lot_slug' => $s['lot_slug'], 'color' => $s['color'], 'lang' => $s['lang']), $atts, 'parkpilot');
    if ($s['host'] === '' || $s['key'] === '' || $a['lot'] === '') {
        return current_user_can('manage_options') ? '<p><em>ParkPilot: finish the setup under Settings &gt; ParkPilot.</em></p>' : '';
    }
    static $n = 0;
    $n++;
    $id = 'parkpilot-widget-' . $n;
    $attrs = array(
        'async' => '', 'src' => $s['host'] . '/widget.js', 'data-key' => $s['key'], 'data-lot' => preg_replace('/\D/', '', $a['lot']),
        'data-tenant' => $s['tenant'], 'data-lot-slug' => sanitize_title($a['lot_slug']), 'data-target' => $id,
        'data-color' => preg_match('/^#[0-9a-fA-F]{6}$/', $a['color']) ? $a['color'] : '#14385C',
    );
    if (preg_match('/^[a-z]{2}$/', $a['lang'])) {
        $attrs['data-lang'] = $a['lang'];
    }
    $html = '';
    foreach ($attrs as $k => $v) {
        $html .= $v === '' ? ' ' . esc_attr($k) : sprintf(' %s="%s"', esc_attr($k), esc_attr($v));
    }
    return '<div id="' . esc_attr($id) . '"></div><script' . $html . '></script>';
});
