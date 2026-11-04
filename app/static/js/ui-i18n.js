/* Interface texts for staff scripts. window.PP_UI maps English text to the page language (filled by the server). */
(function () {
  "use strict";
  window.ppT = function (text, values) {
    var dictionary = window.PP_UI || {};
    var out = Object.prototype.hasOwnProperty.call(dictionary, text) ? dictionary[text] : text;
    if (values) out = out.replace(/%\((\w+)\)s/g, function (match, key) { return key in values ? values[key] : match; });
    return out;
  };
})();
