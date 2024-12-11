// GTK crates
use adw::prelude::*;
use adw::*;
use duct::cmd;
use glib::*;
use serde::Deserialize;
use std::cell::RefCell;
use std::error::Error;
use std::fs;
use std::io::BufRead;
use std::io::BufReader;
use std::process::Command;
use std::rc::Rc;
use std::{thread, time};

#[allow(non_camel_case_types)]
#[derive(PartialEq, Debug, Eq, Hash, Clone, Ord, PartialOrd, Deserialize)]
struct recommended_addons_entry {
    id: i32,
    title: String,
    subtitle: String,
    icon: String,
    pkgman: String,
    checkpkg: String,
    packages: String,
}

fn run_pkcon_command(
    log_loop_sender: async_channel::Sender<String>,
    operation: &str,
    entry_packages: &str,
) -> Result<(), std::boxed::Box<dyn Error + Send + Sync>> {
    let (pipe_reader, pipe_writer) = os_pipe::pipe()?;
    let child = cmd!(
        "bash",
        "-c",
        "/usr/lib/nobara/nobara-welcome/scripts/pkcon-install.sh ".to_owned()
            + operation
            + " "
            + &entry_packages
    )
    .stderr_to_stdout()
    .stdout_file(pipe_writer)
    .start()?;
    for line in BufReader::new(pipe_reader).lines() {
        thread::sleep(time::Duration::from_secs(1));
        log_loop_sender
            .send_blocking(line?)
            .expect("Channel needs to be opened.")
    }
    child.wait()?;

    Ok(())
}

fn run_flatpak_command(
    log_loop_sender: async_channel::Sender<String>,
    operation: &str,
    entry_packages: &str,
) -> Result<(), std::boxed::Box<dyn Error + Send + Sync>> {
    let (pipe_reader, pipe_writer) = os_pipe::pipe()?;
    let child = cmd!(
        "bash",
        "-c",
        "/usr/lib/nobara/nobara-welcome/scripts/flatpak-install.sh ".to_owned()
            + operation
            + " "
            + &entry_packages
    )
    .stderr_to_stdout()
    .stdout_file(pipe_writer)
    .start()?;
    for line in BufReader::new(pipe_reader).lines() {
        thread::sleep(time::Duration::from_secs(1));
        log_loop_sender
            .send_blocking(line?)
            .expect("Channel needs to be opened.")
    }
    child.wait()?;

    Ok(())
}

pub fn recommended_addons_page(
    recommended_addons_content_page_stack: &gtk::Stack,
    window: &adw::ApplicationWindow,
) {

    let recommended_addons_page_box = gtk::Box::builder().vexpand(true).hexpand(true).build();

    let recommended_addons_page_listbox = gtk::ListBox::builder()
        .margin_top(20)
        .margin_bottom(20)
        .margin_start(20)
        .margin_end(20)
        .vexpand(true)
        .hexpand(true)
        .build();
    recommended_addons_page_listbox.add_css_class("boxed-list");

    let recommended_addons_page_scroll = gtk::ScrolledWindow::builder()
        // that puts items vertically
        .hexpand(true)
        .vexpand(true)
        .child(&recommended_addons_page_box)
        .propagate_natural_width(true)
        .propagate_natural_height(true)
        .min_content_width(520)
        .build();

    let entry_buttons_size_group = gtk::SizeGroup::new(gtk::SizeGroupMode::Both);

    let mut json_array: Vec<recommended_addons_entry> = Vec::new();
    let json_path = "/usr/share/nobara/nobara-welcome/config/recommended_addons.json";
    let json_data = fs::read_to_string(json_path).expect("Unable to read json");
    let json_data: serde_json::Value =
        serde_json::from_str(&json_data).expect("JSON format invalid");
    if let serde_json::Value::Array(recommended_addons) = &json_data["recommended_addons"] {
        for recommended_addons_entry in recommended_addons {
            let recommended_addons_entry_struct: recommended_addons_entry =
                serde_json::from_value(recommended_addons_entry.clone()).unwrap();
            json_array.push(recommended_addons_entry_struct);
        }
    }

    for recommended_addons_entry in json_array {
        let (checkpkg_status_loop_sender, checkpkg_status_loop_receiver) =
            async_channel::unbounded();
        let checkpkg_status_loop_sender: async_channel::Sender<bool> =
            checkpkg_status_loop_sender.clone();

        let (log_loop_sender, log_loop_receiver) = async_channel::unbounded();
        let log_loop_sender: async_channel::Sender<String> = log_loop_sender.clone();

        let (log_status_loop_sender, log_status_loop_receiver) = async_channel::unbounded();
        let log_status_loop_sender: async_channel::Sender<bool> = log_status_loop_sender.clone();

        let entry_title = recommended_addons_entry.title;
        let entry_subtitle = recommended_addons_entry.subtitle;
        let entry_icon = recommended_addons_entry.icon;
        let entry_pkgman = recommended_addons_entry.pkgman;
        let entry_checkpkg = recommended_addons_entry.checkpkg;
        let entry_packages = recommended_addons_entry.packages;

        if entry_pkgman == "pkcon" {
            gio::spawn_blocking(
                clone!(@strong checkpkg_status_loop_sender, @strong entry_checkpkg => move || loop {
                    let checkpkg_command = Command::new("rpm")
                        .arg("-q")
                        .arg(&entry_checkpkg)
                        .output()
                        .expect("failed to execute process");
                    if checkpkg_command.status.success() {
                        checkpkg_status_loop_sender.send_blocking(true).expect("The channel needs to be open.");
                    } else {
                        checkpkg_status_loop_sender.send_blocking(false).expect("The channel needs to be open.");
                    }
                    thread::sleep(time::Duration::from_secs(10));
                }),
            );

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
                .vexpand(true)
                .valign(gtk::Align::Center)
                .build();
            entry_buttons_size_group.add_widget(&entry_row_button);
            entry_row.add_prefix(&entry_row_icon);
            entry_row.add_suffix(&entry_row_button);

            let recommended_addons_command_log_terminal_buffer = gtk::TextBuffer::builder().build();

            let recommended_addons_command_log_terminal = gtk::TextView::builder()
                .vexpand(true)
                .hexpand(true)
                .editable(false)
                .buffer(&recommended_addons_command_log_terminal_buffer)
                .build();

            let recommended_addons_command_log_terminal_scroll = gtk::ScrolledWindow::builder()
                .width_request(400)
                .height_request(200)
                .vexpand(true)
                .hexpand(true)
                .child(&recommended_addons_command_log_terminal)
                .build();

            let recommended_addons_command_dialog = adw::MessageDialog::builder()
                .transient_for(window)
                .hide_on_close(true)
                .extra_child(&recommended_addons_command_log_terminal_scroll)
                .width_request(400)
                .height_request(200)
                .heading(t!("recommended_addons_command_dialog_heading"))
                .build();
            recommended_addons_command_dialog.add_response(
                "recommended_addons_command_dialog_ok",
                &t!("recommended_addons_command_dialog_ok_label").to_string(),
            );

            let checkpkg_status_loop_context = MainContext::default();
            // The main loop executes the asynchronous block
            checkpkg_status_loop_context.spawn_local(
                clone!(@weak entry_row_button, @strong checkpkg_status_loop_receiver => async move {
                    while let Ok(state) = checkpkg_status_loop_receiver.recv().await {
                        if state == false {
                            entry_row_button.remove_css_class("destructive-action");
                            entry_row_button.set_label(&t!("entry_row_button_install").to_string());
                            entry_row_button.add_css_class("suggested-action");
                            entry_row_button.set_widget_name("false")
                        } else {
                            entry_row_button.remove_css_class("suggested-action");
                            entry_row_button.set_label(&t!("entry_row_button_remove").to_string());
                            entry_row_button.add_css_class("destructive-action");
                            entry_row_button.set_widget_name("true")
                        }
                    }
                }),
            );

            //
            let log_loop_context = MainContext::default();
            // The main loop executes the asynchronous block
            log_loop_context.spawn_local(clone!(@weak recommended_addons_command_log_terminal_buffer, @weak recommended_addons_command_dialog, @strong log_loop_receiver => async move {
                while let Ok(state) = log_loop_receiver.recv().await {
                    recommended_addons_command_log_terminal_buffer.insert(&mut recommended_addons_command_log_terminal_buffer.end_iter(), &("\n".to_string() + &state))
                }
            }));

            let log_status_loop_context = MainContext::default();
            // The main loop executes the asynchronous block
            log_status_loop_context.spawn_local(clone!(@weak recommended_addons_command_dialog, @strong log_status_loop_receiver => async move {
                        while let Ok(state) = log_status_loop_receiver.recv().await {
                            if state == true {
                                recommended_addons_command_dialog.set_response_enabled("recommended_addons_command_dialog_ok", true);
                                recommended_addons_command_dialog.set_body(&t!("recommended_addons_command_dialog_success_true"));
                            } else {
                                recommended_addons_command_dialog.set_response_enabled("recommended_addons_command_dialog_ok", true);
                                recommended_addons_command_dialog.set_body(&t!("recommended_addons_command_dialog_success_false"));
                            }
                        }
                }));
            //
            recommended_addons_command_log_terminal_buffer.connect_changed(clone!(@weak recommended_addons_command_log_terminal, @weak recommended_addons_command_log_terminal_buffer,@weak recommended_addons_command_log_terminal_scroll => move |_|{
                   if recommended_addons_command_log_terminal_scroll.vadjustment().upper() - recommended_addons_command_log_terminal_scroll.vadjustment().value() > 100.0 {
                        recommended_addons_command_log_terminal_scroll.vadjustment().set_value(recommended_addons_command_log_terminal_scroll.vadjustment().upper())
                    }
                }));

            entry_row_button.connect_clicked(clone!(@strong entry_packages, @weak entry_row_button, @weak window => move |_| {
                    recommended_addons_command_log_terminal_buffer.delete(&mut recommended_addons_command_log_terminal_buffer.bounds().0, &mut recommended_addons_command_log_terminal_buffer.bounds().1);
                    recommended_addons_command_dialog.set_response_enabled("recommended_addons_command_dialog_ok", false);
                    recommended_addons_command_dialog.set_body("");
                    recommended_addons_command_dialog.present();
                    if &entry_row_button.widget_name() == "true" {
                        let log_status_loop_sender_clone = log_status_loop_sender.clone();
                        let log_loop_sender_clone= log_loop_sender.clone();
                        let entry_packages_clone= entry_packages.clone();
                        std::thread::spawn(move || {
                            let command = run_pkcon_command(log_loop_sender_clone, "remove", &entry_packages_clone);
                            match command {
                                Ok(_) => {
                                    println!("Status: Addon Command Successful");
                                    log_status_loop_sender_clone.send_blocking(true).expect("The channel needs to be open.");
                                }
                                Err(_) => {
                                    println!("Status: Addon Command Failed");
                                    log_status_loop_sender_clone.send_blocking(false).expect("The channel needs to be open.");
                                }
                            }
                        });
                    } else {
                        let log_status_loop_sender_clone = log_status_loop_sender.clone();
                        let log_loop_sender_clone= log_loop_sender.clone();
                        let entry_packages_clone= entry_packages.clone();
                        std::thread::spawn(move || {
                            let command = run_pkcon_command(log_loop_sender_clone, "install", &entry_packages_clone);
                            match command {
                                Ok(_) => {
                                    println!("Status: Addon Command Successful");
                                    log_status_loop_sender_clone.send_blocking(true).expect("The channel needs to be open.");
                                }
                                Err(_) => {
                                    println!("Status: Addon Command Failed");
                                    log_status_loop_sender_clone.send_blocking(false).expect("The channel needs to be open.");
                                }
                            }
                        });
                    }
            }));
            recommended_addons_page_listbox.append(&entry_row)
        } else if entry_pkgman == "flatpak" {
            gio::spawn_blocking(
                clone!(@strong checkpkg_status_loop_sender, @strong entry_checkpkg => move || loop {
                    let checkpkg_command = Command::new("/usr/lib/nobara/nobara-welcome/scripts/flatpak-install.sh")
                        .arg("check")
                        .arg(&entry_checkpkg)
                        .output()
                        .expect("failed to execute process");
                    if checkpkg_command.status.success() {
                        checkpkg_status_loop_sender.send_blocking(true).expect("The channel needs to be open.");
                    } else {
                        checkpkg_status_loop_sender.send_blocking(false).expect("The channel needs to be open.");
                    }
                    thread::sleep(time::Duration::from_secs(10));
                }),
            );

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
                .vexpand(true)
                .valign(gtk::Align::Center)
                .build();
            entry_buttons_size_group.add_widget(&entry_row_button);
            entry_row.add_prefix(&entry_row_icon);
            entry_row.add_suffix(&entry_row_button);

            let recommended_addons_command_log_terminal_buffer = gtk::TextBuffer::builder().build();

            let recommended_addons_command_log_terminal = gtk::TextView::builder()
                .vexpand(true)
                .hexpand(true)
                .editable(false)
                .buffer(&recommended_addons_command_log_terminal_buffer)
                .build();

            let recommended_addons_command_log_terminal_scroll = gtk::ScrolledWindow::builder()
                .width_request(400)
                .height_request(200)
                .vexpand(true)
                .hexpand(true)
                .child(&recommended_addons_command_log_terminal)
                .build();

            let recommended_addons_command_dialog = adw::MessageDialog::builder()
                .transient_for(window)
                .hide_on_close(true)
                .extra_child(&recommended_addons_command_log_terminal_scroll)
                .width_request(400)
                .height_request(200)
                .heading(t!("recommended_addons_command_dialog_heading"))
                .build();
            recommended_addons_command_dialog.add_response(
                "recommended_addons_command_dialog_ok",
                &t!("recommended_addons_command_dialog_ok_label").to_string(),
            );

            let checkpkg_status_loop_context = MainContext::default();
            // The main loop executes the asynchronous block
            checkpkg_status_loop_context.spawn_local(
                clone!(@weak entry_row_button, @strong checkpkg_status_loop_receiver => async move {
                    while let Ok(state) = checkpkg_status_loop_receiver.recv().await {
                        if state == false {
                            entry_row_button.remove_css_class("destructive-action");
                            entry_row_button.set_label(&t!("entry_row_button_install").to_string());
                            entry_row_button.add_css_class("suggested-action");
                            entry_row_button.set_widget_name("false")
                        } else {
                            entry_row_button.remove_css_class("suggested-action");
                            entry_row_button.set_label(&t!("entry_row_button_remove").to_string());
                            entry_row_button.add_css_class("destructive-action");
                            entry_row_button.set_widget_name("true")
                        }
                    }
                }),
            );

            //
            let log_loop_context = MainContext::default();
            // The main loop executes the asynchronous block
            log_loop_context.spawn_local(clone!(@weak recommended_addons_command_log_terminal_buffer, @weak recommended_addons_command_dialog, @strong log_loop_receiver => async move {
                while let Ok(state) = log_loop_receiver.recv().await {
                    recommended_addons_command_log_terminal_buffer.insert(&mut recommended_addons_command_log_terminal_buffer.end_iter(), &("\n".to_string() + &state))
                }
            }));

            let log_status_loop_context = MainContext::default();
            // The main loop executes the asynchronous block
            log_status_loop_context.spawn_local(clone!(@weak recommended_addons_command_dialog, @strong log_status_loop_receiver => async move {
                        while let Ok(state) = log_status_loop_receiver.recv().await {
                            if state == true {
                                recommended_addons_command_dialog.set_response_enabled("recommended_addons_command_dialog_ok", true);
                                recommended_addons_command_dialog.set_body(&t!("recommended_addons_command_dialog_success_true"));
                            } else {
                                recommended_addons_command_dialog.set_response_enabled("recommended_addons_command_dialog_ok", true);
                                recommended_addons_command_dialog.set_body(&t!("recommended_addons_command_dialog_success_false"));
                            }
                        }
                }));
            //
            recommended_addons_command_log_terminal_buffer.connect_changed(clone!(@weak recommended_addons_command_log_terminal, @weak recommended_addons_command_log_terminal_buffer,@weak recommended_addons_command_log_terminal_scroll => move |_|{
                   if recommended_addons_command_log_terminal_scroll.vadjustment().upper() - recommended_addons_command_log_terminal_scroll.vadjustment().value() > 100.0 {
                        recommended_addons_command_log_terminal_scroll.vadjustment().set_value(recommended_addons_command_log_terminal_scroll.vadjustment().upper())
                    }
                }));

            entry_row_button.connect_clicked(clone!(@strong entry_packages, @weak entry_row_button, @weak window => move |_| {
                    recommended_addons_command_log_terminal_buffer.delete(&mut recommended_addons_command_log_terminal_buffer.bounds().0, &mut recommended_addons_command_log_terminal_buffer.bounds().1);
                    recommended_addons_command_dialog.set_response_enabled("recommended_addons_command_dialog_ok", false);
                    recommended_addons_command_dialog.set_body("");
                    recommended_addons_command_dialog.present();
                    if &entry_row_button.widget_name() == "true" {
                        let log_status_loop_sender_clone = log_status_loop_sender.clone();
                        let log_loop_sender_clone= log_loop_sender.clone();
                        let entry_packages_clone= entry_packages.clone();
                        std::thread::spawn(move || {
                            let command = run_flatpak_command(log_loop_sender_clone, "remove", &entry_packages_clone);
                            match command {
                                Ok(_) => {
                                    println!("Status: Addon Command Successful");
                                    log_status_loop_sender_clone.send_blocking(true).expect("The channel needs to be open.");
                                }
                                Err(_) => {
                                    println!("Status: Addon Command Failed");
                                    log_status_loop_sender_clone.send_blocking(false).expect("The channel needs to be open.");
                                }
                            }
                        });
                    } else {
                        let log_status_loop_sender_clone = log_status_loop_sender.clone();
                        let log_loop_sender_clone= log_loop_sender.clone();
                        let entry_packages_clone= entry_packages.clone();
                        std::thread::spawn(move || {
                            let command = run_flatpak_command(log_loop_sender_clone, "install", &entry_packages_clone);
                            match command {
                                Ok(_) => {
                                    println!("Status: Addon Command Successful");
                                    log_status_loop_sender_clone.send_blocking(true).expect("The channel needs to be open.");
                                }
                                Err(_) => {
                                    println!("Status: Addon Command Failed");
                                    log_status_loop_sender_clone.send_blocking(false).expect("The channel needs to be open.");
                                }
                            }
                        });
                    }
            }));
            recommended_addons_page_listbox.append(&entry_row)
        }
    }

    recommended_addons_page_box.append(&recommended_addons_page_listbox);

    recommended_addons_content_page_stack.add_titled(
        &recommended_addons_page_scroll,
        Some("recommended_addons_page"),
        &t!("recommended_addons_page_title").to_string(),
    );
}
