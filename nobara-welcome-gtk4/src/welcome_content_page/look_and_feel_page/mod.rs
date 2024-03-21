// GTK crates
use adw::prelude::*;
use adw::*;
use duct::cmd;
use glib::*;
use serde::Deserialize;
use std::cell::RefCell;
use std::path::Path;
use std::rc::Rc;
use std::{env, fs};
use std::{thread, time};

#[allow(non_camel_case_types)]
#[derive(PartialEq, Debug, Eq, Hash, Clone, Ord, PartialOrd, Deserialize)]
struct look_and_feel_entry {
    id: i32,
    title: String,
    subtitle: String,
    icon: String,
    button: String,
    onlyin: String,
    command: String,
}

pub fn look_and_feel_page(
    look_and_feel_content_page_stack: &gtk::Stack,
    window: &adw::ApplicationWindow,
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

    let look_and_feel_page_box = gtk::Box::builder().vexpand(true).hexpand(true).build();

    let look_and_feel_page_listbox = gtk::ListBox::builder()
        .margin_top(20)
        .margin_bottom(20)
        .margin_start(20)
        .margin_end(20)
        .vexpand(true)
        .hexpand(true)
        .build();
    look_and_feel_page_listbox.add_css_class("boxed-list");

    let look_and_feel_page_scroll = gtk::ScrolledWindow::builder()
        // that puts items vertically
        .hexpand(true)
        .vexpand(true)
        .child(&look_and_feel_page_box)
        .propagate_natural_width(true)
        .propagate_natural_height(true)
        .min_content_width(520)
        .build();

    let internet_loop_context = MainContext::default();
    // The main loop executes the asynchronous block
    internet_loop_context.spawn_local(
        clone!(@strong internet_connected_status, @weak look_and_feel_page_box => async move {
            while let Ok(_state) = internet_loop_receiver.recv().await {
                if *internet_connected_status.borrow_mut() == true {
                    look_and_feel_page_box.set_sensitive(true);
                } else {
                    look_and_feel_page_box.set_sensitive(false);
                }
            }
        }),
    );

    let mut json_array: Vec<look_and_feel_entry> = Vec::new();
    let json_path = "/usr/share/nobara/nobara-welcome/config/look_and_feel.json";
    let json_data = fs::read_to_string(json_path).expect("Unable to read json");
    let json_data: serde_json::Value =
        serde_json::from_str(&json_data).expect("JSON format invalid");
    if let serde_json::Value::Array(look_and_feel) = &json_data["look_and_feel"] {
        for look_and_feel_entry in look_and_feel {
            let look_and_feel_entry_struct: look_and_feel_entry =
                serde_json::from_value(look_and_feel_entry.clone()).unwrap();
            json_array.push(look_and_feel_entry_struct);
        }
    }

    let entry_buttons_size_group = gtk::SizeGroup::new(gtk::SizeGroupMode::Both);

    for look_and_feel_entry in json_array {
        let (entry_command_status_loop_sender, entry_command_status_loop_receiver) =
            async_channel::unbounded();
        let entry_command_status_loop_sender: async_channel::Sender<bool> =
            entry_command_status_loop_sender.clone();

        let entry_title = look_and_feel_entry.title;
        let entry_subtitle = look_and_feel_entry.subtitle;
        let entry_icon = look_and_feel_entry.icon;
        let entry_button = look_and_feel_entry.button;
        let entry_onlyin = look_and_feel_entry.onlyin;
        let entry_command = look_and_feel_entry.command;
        let entry_row = adw::ActionRow::builder()
            .title(t!(&entry_title))
            .subtitle(t!(&entry_subtitle))
            .vexpand(true)
            .hexpand(true)
            .build();
        let entry_row_icon = gtk::Image::builder()
            .icon_name(entry_icon)
            .pixel_size(80)
            .vexpand(true)
            .valign(gtk::Align::Center)
            .build();
        let entry_row_button = gtk::Button::builder()
            .label(t!(&entry_button))
            .vexpand(true)
            .valign(gtk::Align::Center)
            .build();
        entry_buttons_size_group.add_widget(&entry_row_button);
        entry_buttons_size_group.add_widget(&entry_row_button);
        entry_row.add_prefix(&entry_row_icon);
        entry_row.add_suffix(&entry_row_button);

        entry_row_button.connect_clicked(clone!(@strong entry_command, @weak window => move |_| {
                gio::spawn_blocking(clone!(@strong entry_command_status_loop_sender, @strong entry_command => move || {
                            if Path::new("/tmp/nobara-welcome-exec.sh").exists() {
                            fs::remove_file("/tmp/nobara-welcome-exec.sh").expect("Bad permissions on /tmp/nobara-welcome-exec.sh");
                            }
                            fs::write("/tmp/nobara-welcome-exec.sh", "#! /bin/bash\nset -e\n".to_owned() + &entry_command).expect("Unable to write file");
                            let _ = cmd!("chmod", "+x", "/tmp/nobara-welcome-exec.sh").read();
                            let command = cmd!("/tmp/nobara-welcome-exec.sh").run();
                            if command.is_err() {
                                entry_command_status_loop_sender.send_blocking(false).expect("The channel needs to be open.");
                            } else {
                                entry_command_status_loop_sender.send_blocking(true).expect("The channel needs to be open.");
                            }
                }));
        }));

        let cmd_err_dialog = adw::MessageDialog::builder()
            .body(t!("cmd_err_dialog_body"))
            .heading(t!("cmd_err_dialog_heading"))
            .hide_on_close(true)
            .transient_for(window)
            .build();
        cmd_err_dialog.add_response(
            "cmd_err_dialog_ok",
            &t!("cmd_err_dialog_ok_label").to_string(),
        );

        let entry_command_status_loop_context = MainContext::default();
        // The main loop executes the asynchronous block
        entry_command_status_loop_context.spawn_local(
            clone!(@weak cmd_err_dialog, @strong entry_command_status_loop_receiver => async move {
                while let Ok(state) = entry_command_status_loop_receiver.recv().await {
                    if state == false {
                        cmd_err_dialog.present();
                    }
                }
            }),
        );
        let current_desktop = match env::var_os("XDG_SESSION_DESKTOP") {
            Some(v) => v.into_string().unwrap(),
            None => panic!("XDG_SESSION_DESKTOP is not set"),
        };
        if entry_onlyin.is_empty() || current_desktop.contains(&entry_onlyin.to_lowercase()) {
            look_and_feel_page_listbox.append(&entry_row)
        }
    }

    look_and_feel_page_box.append(&look_and_feel_page_listbox);

    look_and_feel_content_page_stack.add_titled(
        &look_and_feel_page_scroll,
        Some("look_and_feel_page"),
        &t!("look_and_feel_page_title").to_string(),
    );
}
