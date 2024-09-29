// GTK crates
use crate::config::DISTRO_ICON;
use adw::prelude::*;
use adw::*;
use glib::*;
use std::cell::RefCell;
use std::rc::Rc;
use std::{thread, time};

pub fn welcome_page(
    welcome_content_page_stack: &gtk::Stack,
    window_banner: &adw::Banner,
) {

    let welcome_page_text = adw::StatusPage::builder()
        .icon_name(DISTRO_ICON)
        .title(t!("welcome_page_text_title"))
        .description(t!("welcome_page_text_description"))
        .build();
    welcome_page_text.add_css_class("compact");

    let welcome_page_scroll = gtk::ScrolledWindow::builder()
        // that puts items vertically
        .valign(gtk::Align::Center)
        .hexpand(true)
        .vexpand(true)
        .child(&welcome_page_text)
        .propagate_natural_width(true)
        .propagate_natural_height(true)
        .build();

    welcome_content_page_stack.add_titled(
        &welcome_page_scroll,
        Some("welcome_page"),
        &t!("welcome_page_title").to_string(),
    );
}
