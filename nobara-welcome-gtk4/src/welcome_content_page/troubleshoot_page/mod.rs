// GTK crates
use adw::prelude::*;
use adw::*;
use duct::cmd;
use glib::*;
use serde::Deserialize;
use std::cell::RefCell;
use std::fs;
use std::path::Path;
use std::rc::Rc;
use std::{thread, time};

#[allow(non_camel_case_types)]
#[derive(PartialEq, Debug, Eq, Hash, Clone, Ord, PartialOrd, Deserialize)]
struct troubleshoot_entry {
    id: i32,
    title: String,
    subtitle: String,
    icon: String,
    button: String,
    command: String,
}

pub fn troubleshoot_page(
    troubleshoot_content_page_stack: &gtk::Stack,
    window: &adw::ApplicationWindow,
) {

    let troubleshoot_page_box = gtk::Box::builder().vexpand(true).hexpand(true).build();

    let troubleshoot_page_listbox = gtk::ListBox::builder()
        .margin_top(20)
        .margin_bottom(20)
        .margin_start(20)
        .margin_end(20)
        .vexpand(true)
        .hexpand(true)
        .build();
    troubleshoot_page_listbox.add_css_class("boxed-list");

    let troubleshoot_page_scroll = gtk::ScrolledWindow::builder()
        // that puts items vertically
        .hexpand(true)
        .vexpand(true)
        .child(&troubleshoot_page_box)
        .propagate_natural_width(true)
        .propagate_natural_height(true)
        .min_content_width(520)
        .build();

    let mut json_array: Vec<troubleshoot_entry> = Vec::new();
    let json_path = "/usr/share/nobara/nobara-welcome/config/troubleshoot.json";
    let json_data = fs::read_to_string(json_path).expect("Unable to read json");
    let json_data: serde_json::Value =
        serde_json::from_str(&json_data).expect("JSON format invalid");
    if let serde_json::Value::Array(troubleshoot) = &json_data["troubleshoot"] {
        for troubleshoot_entry in troubleshoot {
            let troubleshoot_entry_struct: troubleshoot_entry =
                serde_json::from_value(troubleshoot_entry.clone()).unwrap();
            json_array.push(troubleshoot_entry_struct);
        }
    }

    let entry_buttons_size_group = gtk::SizeGroup::new(gtk::SizeGroupMode::Both);

    for troubleshoot_entry in json_array {
        let (entry_command_status_loop_sender, entry_command_status_loop_receiver) =
            async_channel::unbounded();
        let entry_command_status_loop_sender: async_channel::Sender<bool> =
            entry_command_status_loop_sender.clone();

        let entry_title = troubleshoot_entry.title;
        let entry_subtitle = troubleshoot_entry.subtitle;
        let entry_icon = troubleshoot_entry.icon;
        let entry_button = troubleshoot_entry.button;
        let entry_command = troubleshoot_entry.command;
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
        troubleshoot_page_listbox.append(&entry_row)
    }

    troubleshoot_page_box.append(&troubleshoot_page_listbox);

    troubleshoot_content_page_stack.add_titled(
        &troubleshoot_page_scroll,
        Some("troubleshoot_page"),
        &t!("troubleshoot_page_title").to_string(),
    );
}
