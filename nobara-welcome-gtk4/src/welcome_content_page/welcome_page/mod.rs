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
    internet_connected: &Rc<RefCell<bool>>,
) {
    let internet_connected_status = internet_connected.clone();

    let (internet_loop_sender, internet_loop_receiver) = async_channel::unbounded();
    let internet_loop_sender = internet_loop_sender.clone();
    // The long running operation runs now in a separate thread
    gio::spawn_blocking(move || loop {
        thread::sleep(time::Duration::from_secs(1));
        internet_loop_sender
            .send_blocking(true)
            .expect("The channel needs to be open.");
    });

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

    let internet_loop_context = MainContext::default();
    // The main loop executes the asynchronous block
    internet_loop_context.spawn_local(
        clone!(@strong internet_connected_status, @weak window_banner => async move {
            while let Ok(_state) = internet_loop_receiver.recv().await {
                if *internet_connected_status.borrow_mut() == true {
                    window_banner.set_revealed(false);
                } else {
                    window_banner.set_revealed(true);
                    window_banner.set_title(&t!("window_banner_no_internet"));
                }
            }
        }),
    );

    welcome_content_page_stack.add_titled(
        &welcome_page_scroll,
        Some("welcome_page"),
        &t!("welcome_page_title").to_string(),
    );
}
