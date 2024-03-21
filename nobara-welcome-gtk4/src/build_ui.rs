// GTK crates
/// Use all gtk4 libraries (gtk4 -> gtk because cargo)
/// Use all libadwaita libraries (libadwaita -> adw because cargo)

// application crates
/// first setup crates
use crate::config::*;
use crate::save_window_size::save_window_size;
use crate::welcome_content_page::welcome_content_page;
use adw::prelude::*;
use adw::*;
use gtk::Orientation;

pub fn build_ui(app: &adw::Application) {
    // setup glib
    gtk::glib::set_prgname(Some(t!("app_name").to_string()));
    glib::set_application_name(&t!("app_name").to_string());
    let glib_settings = gio::Settings::new(APP_ID);

    let content_box = gtk::Box::builder()
        .vexpand(true)
        .hexpand(true)
        .orientation(Orientation::Vertical)
        .build();

    // create the main Application window
    let window = adw::ApplicationWindow::builder()
        .title(t!("app_name"))
        .application(app)
        .content(&content_box)
        .icon_name(APP_ICON)
        .default_width(glib_settings.int("window-width"))
        .default_height(glib_settings.int("window-height"))
        .width_request(430)
        .height_request(500)
        .startup_id(APP_ID)
        .build();

    if glib_settings.boolean("is-maximized") == true {
        window.maximize()
    }

    window.connect_close_request(move |window| {
        if let Some(application) = window.application() {
            save_window_size(&window, &glib_settings);
            application.remove_window(window);
        }
        glib::Propagation::Proceed
    });

    //
    welcome_content_page(&window, &content_box);
    // show the window
    window.present()
}
