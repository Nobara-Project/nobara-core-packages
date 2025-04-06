mod build_ui;
mod config;
mod save_window_size;
use config::*;

use adw::prelude::*;
use adw::*;
use gdk::Display;
use gtk::*;
use sys_locale::get_locale;


#[derive(PartialEq, Debug, Eq, Hash, Clone, Ord, PartialOrd)]
pub struct DriverPackage {
    driver: String,
    version: String,
    device: String,
    description: String,
    icon: String,
    experimental: bool,
    removeble: bool,
}

use build_ui::build_ui;

use std::boxed::Box;

// Init translations for current crate.
#[macro_use]
extern crate rust_i18n;
i18n!("locales", fallback = "en_US");

/// main function
fn main() {
    let current_locale = get_locale().unwrap_or_else(|| String::from("en-US")).replace("-", "_");

    rust_i18n::set_locale(&current_locale);
    let application = adw::Application::new(Some(APP_ID), Default::default());
    application.connect_startup(|app| {
        // The CSS "magic" happens here.
        let provider = CssProvider::new();
        provider.load_from_string(include_str!("style.css"));
        // We give the CssProvided to the default screen so the CSS rules we added
        // can be applied to our window.
        gtk::style_context_add_provider_for_display(
            &Display::default().expect("Could not connect to a display."),
            &provider,
            STYLE_PROVIDER_PRIORITY_APPLICATION,
        );
        app.connect_activate(build_ui);
    });

    application.run();
}
