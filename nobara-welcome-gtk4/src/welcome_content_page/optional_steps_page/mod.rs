// GTK crates
use adw::prelude::*;
use adw::*;
use duct::cmd;
use glib::*;
use serde::Deserialize;
use std::cell::RefCell;
use std::error::Error;
use std::path::Path;
use std::fs;
use std::io::BufRead;
use std::io::BufReader;
use std::rc::Rc;
use std::{thread, time};

#[allow(non_camel_case_types)]
#[derive(PartialEq, Debug, Eq, Hash, Clone, Ord, PartialOrd, Deserialize)]
struct optional_steps_entry {
    id: i32,
    title: String,
    subtitle: String,
    icon: String,
    button: String,
    terminal: bool,
    command: String,
}

fn run_with_gui(
    log_loop_sender: async_channel::Sender<String>,
    command: &str,
) -> Result<(), std::boxed::Box<dyn Error + Send + Sync>> {
    let (pipe_reader, pipe_writer) = os_pipe::pipe()?;
    let child = cmd!(
        "bash",
        "-c",
        command
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

pub fn optional_steps_page(
    optional_steps_content_page_stack: &gtk::Stack,
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

    let optional_steps_page_box = gtk::Box::builder().vexpand(true).hexpand(true).build();

    let optional_steps_page_listbox = gtk::ListBox::builder()
        .margin_top(20)
        .margin_bottom(20)
        .margin_start(20)
        .margin_end(20)
        .vexpand(true)
        .hexpand(true)
        .build();
    optional_steps_page_listbox.add_css_class("boxed-list");

    let optional_steps_page_scroll = gtk::ScrolledWindow::builder()
        // that puts items vertically
        .hexpand(true)
        .vexpand(true)
        .child(&optional_steps_page_box)
        .propagate_natural_width(true)
        .propagate_natural_height(true)
        .min_content_width(520)
        .build();

    let internet_loop_context = MainContext::default();
    // The main loop executes the asynchronous block
    internet_loop_context.spawn_local(
        clone!(@strong internet_connected_status, @weak optional_steps_page_box => async move {
            while let Ok(_state) = internet_loop_receiver.recv().await {
                if *internet_connected_status.borrow_mut() == true {
                    optional_steps_page_box.set_sensitive(true);
                } else {
                    optional_steps_page_box.set_sensitive(false);
                }
            }
        }),
    );

    let mut json_array: Vec<optional_steps_entry> = Vec::new();
    let json_path = "/usr/share/nobara/nobara-welcome/config/optional_steps.json";
    let json_data = fs::read_to_string(json_path).expect("Unable to read json");
    let json_data: serde_json::Value =
        serde_json::from_str(&json_data).expect("JSON format invalid");
    if let serde_json::Value::Array(optional_steps) = &json_data["optional_steps"] {
        for optional_steps_entry in optional_steps {
            let optional_steps_entry_struct: optional_steps_entry =
                serde_json::from_value(optional_steps_entry.clone()).unwrap();
            json_array.push(optional_steps_entry_struct);
        }
    }

    let entry_buttons_size_group = gtk::SizeGroup::new(gtk::SizeGroupMode::Both);

    for optional_steps_entry in json_array {
        let (entry_command_status_loop_sender, entry_command_status_loop_receiver) =
            async_channel::unbounded();
        let entry_command_status_loop_sender: async_channel::Sender<bool> =
            entry_command_status_loop_sender.clone();

        let entry_title = optional_steps_entry.title;
        let entry_subtitle = optional_steps_entry.subtitle;
        let entry_icon = optional_steps_entry.icon;
        let entry_button = optional_steps_entry.button;
        let entry_with_terminal = optional_steps_entry.terminal;
        let entry_command = optional_steps_entry.command;
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

        if entry_with_terminal {
            let (log_loop_sender, log_loop_receiver) = async_channel::unbounded();
            let log_loop_sender: async_channel::Sender<String> = log_loop_sender.clone();
    
            let (log_status_loop_sender, log_status_loop_receiver) = async_channel::unbounded();
            let log_status_loop_sender: async_channel::Sender<bool> = log_status_loop_sender.clone();
            let step_step_update_command_log_terminal_buffer = gtk::TextBuffer::builder().build();

            let step_step_update_command_log_terminal = gtk::TextView::builder()
                .vexpand(true)
                .hexpand(true)
                .editable(false)
                .buffer(&step_step_update_command_log_terminal_buffer)
                .build();
    
            let step_step_update_command_log_terminal_scroll = gtk::ScrolledWindow::builder()
                .width_request(800)
                .height_request(400)
                .vexpand(true)
                .hexpand(true)
                .child(&step_step_update_command_log_terminal)
                .build();
    
            let step_step_update_command_dialog = adw::MessageDialog::builder()
                .transient_for(window)
                .hide_on_close(true)
                .extra_child(&step_step_update_command_log_terminal_scroll)
                .width_request(400)
                .height_request(200)
                .heading(t!("step_step_update_command_dialog_heading"))
                .build();
            step_step_update_command_dialog.add_response(
                "step_step_update_command_dialog_ok",
                &t!("step_step_update_command_dialog_ok_label").to_string(),
            );
    
            //
            let log_loop_context = MainContext::default();
            // The main loop executes the asynchronous block
            log_loop_context.spawn_local(clone!(@weak step_step_update_command_log_terminal_buffer, @weak step_step_update_command_dialog, @strong log_loop_receiver => async move {
                while let Ok(state) = log_loop_receiver.recv().await {
                    step_step_update_command_log_terminal_buffer.insert(&mut step_step_update_command_log_terminal_buffer.end_iter(), &("\n".to_string() + &state))
                }
            }));
    
            let log_status_loop_context = MainContext::default();
            // The main loop executes the asynchronous block
            log_status_loop_context.spawn_local(clone!(@weak step_step_update_command_dialog, @strong log_status_loop_receiver => async move {
                        while let Ok(state) = log_status_loop_receiver.recv().await {
                            if state == true {
                                step_step_update_command_dialog.set_response_enabled("step_step_update_command_dialog_ok", true);
                                step_step_update_command_dialog.set_body(&t!("step_step_update_command_dialog_success_true"));
                            } else {
                                step_step_update_command_dialog.set_response_enabled("step_step_update_command_dialog_ok", true);
                                step_step_update_command_dialog.set_body(&t!("step_step_update_command_dialog_success_false"));
                            }
                        }
                }));
            
            step_step_update_command_log_terminal_buffer.connect_changed(clone!(@weak step_step_update_command_log_terminal, @weak step_step_update_command_log_terminal_buffer,@weak step_step_update_command_log_terminal_scroll => move |_|{
                   if step_step_update_command_log_terminal_scroll.vadjustment().upper() - step_step_update_command_log_terminal_scroll.vadjustment().value() > 100.0 {
                        step_step_update_command_log_terminal_scroll.vadjustment().set_value(step_step_update_command_log_terminal_scroll.vadjustment().upper())
                    }
                }));
    
            entry_row_button.connect_clicked(clone!(@strong entry_command, @weak entry_row_button, @weak window => move |_| {
                step_step_update_command_log_terminal_buffer.delete(&mut step_step_update_command_log_terminal_buffer.bounds().0, &mut step_step_update_command_log_terminal_buffer.bounds().1);
                step_step_update_command_dialog.set_response_enabled("step_step_update_command_dialog_ok", false);
                step_step_update_command_dialog.set_body("");
                step_step_update_command_dialog.present();
                    let log_status_loop_sender_clone = log_status_loop_sender.clone();
                    let log_loop_sender_clone= log_loop_sender.clone();
                    let entry_command_clone= entry_command.clone();
                    std::thread::spawn(move || {
                        let command = run_with_gui(log_loop_sender_clone, &entry_command_clone);
                        match command {
                            Ok(_) => {
                                println!("Status: Update Command Successful");
                                log_status_loop_sender_clone.send_blocking(true).expect("The channel needs to be open.");
                            }
                            Err(_) => {
                                println!("Status: Update Command Failed");
                                log_status_loop_sender_clone.send_blocking(false).expect("The channel needs to be open.");
                            }
                        }
                    });
            }));
        } else {
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
        }


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
        optional_steps_page_listbox.append(&entry_row)
    }

    optional_steps_page_box.append(&optional_steps_page_listbox);

    optional_steps_content_page_stack.add_titled(
        &optional_steps_page_scroll,
        Some("optional_steps_page"),
        &t!("optional_steps_page_title").to_string(),
    );
}
