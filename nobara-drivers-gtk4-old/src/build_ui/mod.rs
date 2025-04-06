use std::thread;
use crate::config::*;
use crate::save_window_size::save_window_size;
use crate::DriverPackage;
use adw::glib::{clone, MainContext};
use adw::prelude::*;
use adw::{gio, glib};
use duct::cmd;
use gtk::prelude::{BoxExt, ButtonExt, GtkWindowExt, WidgetExt};
use gtk::{Orientation, SizeGroupMode};
use std::collections::HashMap;
use std::error::Error;
use std::fs;
use std::io::BufRead;
use std::io::BufReader;
use std::path::Path;
use std::process::Command;
use std::time::Duration;

use users::*;

pub fn build_ui(app: &adw::Application) {
    gtk::glib::set_prgname(Some(t!("app_name").to_string()));
    glib::set_application_name(&t!("app_name").to_string());
    let glib_settings = gio::Settings::new(APP_ID);

    let content_box = gtk::Box::builder()
    .orientation(Orientation::Vertical)
    .vexpand(true)
    .hexpand(true)
    .build();

    let loading_box = gtk::Box::builder()
    .orientation(Orientation::Vertical)
    .margin_top(20)
    .margin_bottom(20)
    .margin_start(20)
    .margin_end(20)
    .vexpand(true)
    .hexpand(true)
    .build();

    let loading_icon = gtk::Image::builder()
    .icon_name(APP_ICON)
    .margin_top(20)
    .margin_bottom(20)
    .margin_start(20)
    .vexpand(true)
    .hexpand(true)
    .margin_end(20)
    .pixel_size(256)
    .build();

    let loading_spinner = gtk::Spinner::builder()
    .margin_top(20)
    .margin_bottom(20)
    .margin_start(20)
    .width_request(120)
    .height_request(120)
    .margin_end(20)
    .build();

    let loading_label = gtk::Label::builder()
    .label(t!("loading_label_label"))
    .margin_top(20)
    .margin_bottom(20)
    .margin_start(20)
    .vexpand(true)
    .hexpand(true)
    .margin_end(20)
    .build();

    loading_spinner.start();

    loading_box.append(&loading_icon);
    loading_box.append(&loading_spinner);
    loading_box.append(&loading_label);

    let window = adw::ApplicationWindow::builder()
    .title(t!("app_name"))
    .application(app)
    .content(&content_box)
    .icon_name(APP_ICON)
    .default_width(glib_settings.int("window-width"))
    .default_height(glib_settings.int("window-height"))
    .width_request(950)
    .height_request(500)
    .startup_id(APP_ID)
    .build();

    if glib_settings.boolean("is-maximized") == true {
        window.maximize()
    }

    let window_title_bar = gtk::HeaderBar::builder().show_title_buttons(true).build();

    let credits_button = gtk::Button::builder()
    .icon_name("dialog-information-symbolic")
    .build();

    let credits_window = adw::AboutWindow::builder()
    .application_icon(APP_ICON)
    .application_name(t!("app_name"))
    .transient_for(&window)
    .version(VERSION)
    .hide_on_close(true)
    .developer_name(t!("app_dev"))
    .issue_url(APP_GITHUB.to_owned() + "/issues")
    .build();

    let list_row_size_group = gtk::SizeGroup::new(SizeGroupMode::Horizontal);
    let rows_size_group = gtk::SizeGroup::new(SizeGroupMode::Both);

    content_box.append(&window_title_bar);

    window_title_bar.pack_end(&credits_button.clone());

    window.connect_close_request(move |window| {
        if let Some(application) = window.application() {
            save_window_size(&window, &glib_settings);
            application.remove_window(window);
        }
        glib::Propagation::Proceed
    });

    credits_button
    .connect_clicked(clone!(@weak credits_button => move |_| credits_window.present()));

    println!("Downloading driver DB...");
    content_box.append(&loading_box);
    let data = reqwest::blocking::get(DRIVER_DB_JSON_URL)
        .unwrap()
        .text()
        .unwrap();
    let (drive_hws_sender, drive_hws_receiver) = async_channel::unbounded();
    let drive_hws_sender = drive_hws_sender.clone();
    // The long running operation runs now in a separate thread
    gio::spawn_blocking(clone!(@strong data => move || {
        let mut driver_package_array: Vec<DriverPackage> = Vec::new();
        println!("Parsing Downloaded driver DB...");
        let res: serde_json::Value = serde_json::from_str(&data).expect("Unable to parse");
        if let serde_json::Value::Array(drivers) = &res["drivers"] {
            for driver in drivers {
                if Path::new("/tmp/run-pkdm-detect.sh").exists() {
                    fs::remove_file("/tmp/run-pkdm-detect.sh").expect("Bad permissions on /tmp/nobara-installer-gtk4-target-manual.txt");
                }
                fs::write("/tmp/run-pkdm-detect.sh", "#! /bin/bash\nset -e\nexport LANG=en_US.UTF-8\n".to_owned() + driver["detection"].as_str().to_owned().unwrap()).expect("Unable to write file");
                let _ = cmd!("chmod", "+x", "/tmp/run-pkdm-detect.sh").read();
                let result = cmd!("/tmp/run-pkdm-detect.sh").stdout_capture().read();
                if result.is_ok() {
                    let driver_name = driver["driver"].as_str().to_owned().unwrap().to_string();
                    let driver_device = result.unwrap();
                    let driver_icon = driver["icon"].as_str().to_owned().unwrap().to_string();
                    let driver_experimental = driver["experimental"].as_bool().unwrap();
                    let driver_removeble= driver["removable"].as_bool().unwrap();
                    let command_version_label = Command::new("/usr/lib/nobara/drivers/generate_package_info.sh")
                        .args(["version", &driver_name])
                        .output()
                        .unwrap();
                    let command_description_label = Command::new("/usr/lib/nobara/drivers/generate_package_info.sh")
                        .args(["description", &driver_name])
                        .output()
                        .unwrap();
                    let found_driver_package = DriverPackage {
                        driver: driver_name,
                        version: String::from_utf8(command_version_label.stdout)
                            .unwrap()
                            .trim()
                            .to_string(),
                        device: driver_device,
                        description: String::from_utf8(command_description_label.stdout)
                            .unwrap()
                            .trim()
                            .to_string(),
                        icon: driver_icon,
                        experimental: driver_experimental,
                        removeble: driver_removeble,
                        };
                        driver_package_array.push(found_driver_package)
                    }
                }
            }
            //driver_array.sort_by(|a, b| b.cmp(a))
            driver_package_array.sort_by(|a, b| b.cmp(a));

            drive_hws_sender
                .send_blocking(driver_package_array)
                .expect("channel needs to be open.")
        }));

        window.present();

        let drive_hws_main_context = MainContext::default();
        // The main loop executes the asynchronous block
        drive_hws_main_context.spawn_local(
            clone!(@weak content_box, @weak loading_box, @strong data => async move {
                while let Ok(drive_hws_state) = drive_hws_receiver.recv().await {
                    get_drivers(&content_box, &loading_box, drive_hws_state, &window, &list_row_size_group, &rows_size_group);
                }
            }),
        );
}

const DRIVER_MODIFY_PROG: &str = r###"
#! /bin/bash
DRIVER="$0"
export SUDO_USER=$USER
pkexec env SUDO_USER=$SUDO_USER /usr/lib/nobara/drivers/modify-driver.sh "${DRIVER}"
if [[ $DRIVER == "xone" ]]; then
    newgrp pkg-build <<EOF
lpf reset xone-firmware
lpf update xone-firmware
EOF
fi
"###;
fn driver_modify(
    log_loop_sender: async_channel::Sender<String>,
    driver_pkg: &str,
) -> Result<(), std::boxed::Box<dyn Error + Send + Sync>> {
    let (pipe_reader, pipe_writer) = os_pipe::pipe()?;
    let child = cmd!("bash", "-c", DRIVER_MODIFY_PROG, driver_pkg)
    .stderr_to_stdout()
    .stdout_file(pipe_writer)
    .start()?;
    for line in BufReader::new(pipe_reader).lines() {
        log_loop_sender
        .send_blocking(line?)
        .expect("Channel needs to be opened.")
    }
    child.wait()?;

    Ok(())
}

fn get_drivers(
    main_window: &gtk::Box,
    loading_box: &gtk::Box,
    driver_array: Vec<DriverPackage>,
    window: &adw::ApplicationWindow,
    list_row_size_group: &gtk::SizeGroup,
    rows_size_group: &gtk::SizeGroup,
) {
    let main_box = gtk::Box::builder()
    .margin_top(20)
    .margin_bottom(20)
    .margin_start(20)
    .margin_end(20)
    .hexpand(true)
    .vexpand(true)
    .orientation(Orientation::Vertical)
    .halign(gtk::Align::Center)
    .valign(gtk::Align::Center)
    .build();

    let main_scroll = gtk::ScrolledWindow::builder()
    .max_content_width(650)
    .min_content_width(300)
    .hscrollbar_policy(gtk::PolicyType::Never)
    .child(&main_box)
    .build();

    let window_box = gtk::Box::builder()
    .orientation(Orientation::Vertical)
    .build();

    window_box.append(&main_scroll);

    let mut device_groups: HashMap<String, Vec<DriverPackage>> = HashMap::new();

    if !driver_array.is_empty() {
        driver_array.into_iter().for_each(|driver_package| {
            let group = device_groups
            .entry(driver_package.clone().device.to_owned())
            .or_insert(vec![]);
            group.push(driver_package);
        });
        for (device, group) in device_groups {
            let device_label = gtk::Label::builder()
            .label(t!("device_label_label_prefix").to_owned().to_string() + &device)
            .halign(gtk::Align::Center)
            .valign(gtk::Align::Center)
            .build();
            device_label.add_css_class("deviceLabel");

            main_box.append(&device_label);

            let drivers_list_row = gtk::ListBox::builder()
            .margin_top(20)
            .margin_bottom(20)
            .margin_start(20)
            .margin_end(20)
            .vexpand(true)
            .hexpand(true)
            .build();
            drivers_list_row.add_css_class("boxed-list");

            list_row_size_group.add_widget(&drivers_list_row);

            main_box.append(&drivers_list_row);

            for driver in group.iter() {
                let (log_loop_sender, log_loop_receiver) = async_channel::unbounded();
                let log_loop_sender: async_channel::Sender<String> = log_loop_sender.clone();

                let (log_status_loop_sender, log_status_loop_receiver) = async_channel::unbounded();
                let log_status_loop_sender: async_channel::Sender<bool> =
                log_status_loop_sender.clone();

                let (driver_status_loop_sender, driver_status_loop_receiver) = async_channel::unbounded();
                let driver_status_loop_sender: async_channel::Sender<bool> =
                driver_status_loop_sender.clone();

                let driver_package_ind2 = driver.driver.to_owned();
                let driver_package_removeble = driver.removeble.to_owned();

                gio::spawn_blocking(move || loop {
                    let command_installed_status = Command::new("rpm")
                    .args(["-q", &driver_package_ind2])
                    .output()
                    .unwrap();
                    if command_installed_status.status.success() {
                        driver_status_loop_sender.send_blocking(true).expect("channel needs to be open")
                    } else {
                        driver_status_loop_sender.send_blocking(false).expect("channel needs to be open")
                    }
                    thread::sleep(Duration::from_secs(10));
                });

                let driver_package_ind = driver.driver.to_owned();
                let driver_expander_row = adw::ExpanderRow::new();
                let driver_icon = gtk::Image::builder()
                .icon_name(driver.clone().icon)
                .pixel_size(32)
                .build();
                let driver_status_icon = gtk::Image::builder()
                .icon_name("emblem-default")
                .pixel_size(24)
                .visible(false)
                .tooltip_text(t!("driver_status_icon_tooltip_text"))
                .build();
                let driver_description_label = gtk::Label::builder()
                .label(driver.clone().description)
                .build();
                let driver_content_row = adw::ActionRow::builder().build();
                let driver_install_button = gtk::Button::builder()
                .margin_start(5)
                .margin_top(5)
                .margin_bottom(5)
                .valign(gtk::Align::Center)
                .label(t!("driver_install_button_label"))
                .tooltip_text(t!("driver_install_button_tooltip_text"))
                .sensitive(false)
                .build();
                driver_install_button.add_css_class("suggested-action");
                let driver_remove_button = gtk::Button::builder()
                .margin_end(5)
                .margin_top(5)
                .margin_bottom(5)
                .valign(gtk::Align::Center)
                .label(t!("driver_remove_button_label"))
                .tooltip_text(t!("driver_remove_button_tooltip_text"))
                .sensitive(false)
                .build();
                let driver_action_box = gtk::Box::builder().homogeneous(true).build();
                driver_remove_button.add_css_class("destructive-action");
                driver_expander_row.add_prefix(&driver_icon);
                driver_expander_row.add_suffix(&driver_status_icon);
                if driver.clone().experimental == true {
                    driver_expander_row.set_title(
                        &(driver.clone().driver
                        + &t!("driver_expander_row_title_suffix").to_string()),
                    );
                    driver_expander_row.add_css_class("midLabelWARN");
                } else {
                    driver_expander_row.set_title(&driver.clone().driver);
                }
                driver_expander_row.set_subtitle(&driver.clone().version);
                //
                driver_content_row.add_prefix(&driver_description_label);
                driver_action_box.append(&driver_remove_button);
                driver_action_box.append(&driver_install_button);
                driver_content_row.add_suffix(&driver_action_box);
                driver_expander_row.add_row(&driver_content_row);
                rows_size_group.add_widget(&driver_action_box);
                //
                let driver_status_loop_context = MainContext::default();
                // The main loop executes the asynchronous block
                driver_status_loop_context.spawn_local(clone!(@weak driver_remove_button, @weak driver_install_button, @strong driver_status_loop_receiver => async move {
                    while let Ok(driver_status_state) = driver_status_loop_receiver.recv().await {
                        if driver_status_state == true {
                            driver_status_icon.set_visible(true);
                            driver_install_button.set_sensitive(false);
                            if driver_package_removeble == true {
                                driver_remove_button.set_sensitive(true);
                            }
                        } else {
                            driver_status_icon.set_visible(false);
                            driver_remove_button.set_sensitive(false);
                            driver_install_button.set_sensitive(true);
                        }
                    }
                }));
                //
                let driver_install_log_terminal_buffer = gtk::TextBuffer::builder().build();

                let driver_install_log_terminal = gtk::TextView::builder()
                .vexpand(true)
                .hexpand(true)
                .editable(false)
                .buffer(&driver_install_log_terminal_buffer)
                .build();

                let driver_install_log_terminal_scroll = gtk::ScrolledWindow::builder()
                .width_request(400)
                .height_request(200)
                .vexpand(true)
                .hexpand(true)
                .child(&driver_install_log_terminal)
                .build();

                let driver_install_dialog = adw::MessageDialog::builder()
                .transient_for(window)
                .hide_on_close(true)
                .extra_child(&driver_install_log_terminal_scroll)
                .width_request(400)
                .height_request(200)
                .heading(t!("driver_install_dialog_heading"))
                .build();
                driver_install_dialog.add_response(
                    "driver_install_dialog_ok",
                    &t!("driver_install_dialog_ok_label").to_string(),
                );
                driver_install_dialog.add_response(
                    "driver_install_dialog_reboot",
                    &t!("driver_install_dialog_reboot_label").to_string(),
                );
                driver_install_dialog.set_response_appearance(
                    "driver_install_dialog_reboot",
                    adw::ResponseAppearance::Suggested,
                );
                //

                //
                let log_loop_context = MainContext::default();
                // The main loop executes the asynchronous block
                log_loop_context.spawn_local(clone!(@weak driver_install_log_terminal_buffer, @weak driver_install_dialog, @strong log_loop_receiver => async move {
                    while let Ok(state) = log_loop_receiver.recv().await {
                        driver_install_log_terminal_buffer.insert(&mut driver_install_log_terminal_buffer.end_iter(), &("\n".to_string() + &state))
                    }
                }));

                let log_status_loop_context = MainContext::default();
                // The main loop executes the asynchronous block
                log_status_loop_context.spawn_local(clone!(@weak driver_install_dialog, @strong log_status_loop_receiver => async move {
                    while let Ok(state) = log_status_loop_receiver.recv().await {
                        if state == true {
                            driver_install_dialog.set_response_enabled("driver_install_dialog_ok", true);
                            if get_current_username().unwrap() == "liveuser" {
                                driver_install_dialog.set_response_enabled("driver_install_dialog_reboot", false);
                            } else {
                                driver_install_dialog.set_response_enabled("driver_install_dialog_reboot", true);
                            }
                            driver_install_dialog.set_body(&t!("driver_install_dialog_success_true"));
                        } else {
                            driver_install_dialog.set_response_enabled("driver_install_dialog_ok", true);
                            driver_install_dialog.set_body(&t!("driver_install_dialog_success_false"));
                            driver_install_dialog.set_response_enabled("driver_install_dialog_reboot", false);
                        }
                    }
                }));
                //
                driver_install_log_terminal_buffer.connect_changed(clone!(@weak driver_install_log_terminal, @weak driver_install_log_terminal_buffer,@weak driver_install_log_terminal_scroll => move |_|{
                    if driver_install_log_terminal_scroll.vadjustment().upper() - driver_install_log_terminal_scroll.vadjustment().value() > 100.0 {
                        driver_install_log_terminal_scroll.vadjustment().set_value(driver_install_log_terminal_scroll.vadjustment().upper())
                    }
                }));
                //
                driver_install_button.connect_clicked(clone!(@weak driver_install_log_terminal,@weak driver_install_log_terminal_buffer, @weak driver_install_dialog, @strong log_loop_sender, @strong log_status_loop_sender, @strong driver_package_ind => move |_| {
                    driver_install_log_terminal_buffer.delete(&mut driver_install_log_terminal_buffer.bounds().0, &mut driver_install_log_terminal_buffer.bounds().1);
                    driver_install_dialog.set_response_enabled("driver_install_dialog_ok", false);
                    driver_install_dialog.set_response_enabled("driver_install_dialog_reboot", false);
                    driver_install_dialog.set_body("");
                    driver_install_dialog.choose(None::<&gio::Cancellable>, move |choice| {
                        if choice == "driver_install_dialog_reboot" {
                            Command::new("systemctl")
                            .arg("reboot")
                            .spawn()
                            .expect("systemctl reboot failed to start");
                        }
                    });
                    let log_status_loop_sender_clone = log_status_loop_sender.clone();
                    let log_loop_sender_clone= log_loop_sender.clone();
                    let driver_package_ind_clone = driver_package_ind.clone();
                    std::thread::spawn(move || {
                        let command = driver_modify(log_loop_sender_clone, &driver_package_ind_clone);
                        match command {
                            Ok(_) => {
                                println!("Status: Driver modify Successful");
                                log_status_loop_sender_clone.send_blocking(true).expect("The channel needs to be open.");
                            }
                            Err(_) => {
                                println!("Status: Driver modify Failed");
                                log_status_loop_sender_clone.send_blocking(false).expect("The channel needs to be open.");
                            }
                        }
                    });
                }));
                driver_remove_button.connect_clicked(clone!(@weak driver_install_log_terminal,@weak driver_install_log_terminal_buffer, @weak driver_install_dialog, @strong log_loop_sender, @strong log_status_loop_sender, @strong driver_package_ind  => move |_| {
                    driver_install_log_terminal_buffer.delete(&mut driver_install_log_terminal_buffer.bounds().0, &mut driver_install_log_terminal_buffer.bounds().1);
                    driver_install_dialog.set_response_enabled("driver_install_dialog_ok", false);
                    driver_install_dialog.set_response_enabled("driver_install_dialog_reboot", false);
                    driver_install_dialog.set_body("");
                    driver_install_dialog.choose(None::<&gio::Cancellable>, move |choice| {
                        if choice == "driver_install_dialog_reboot" {
                            Command::new("systemctl")
                            .arg("reboot")
                            .spawn()
                            .expect("systemctl reboot failed to start");
                        }
                    });
                    let log_status_loop_sender_clone = log_status_loop_sender.clone();
                    let log_loop_sender_clone= log_loop_sender.clone();
                    let driver_package_ind_clone = driver_package_ind.clone();
                    std::thread::spawn(move || {
                        let command = driver_modify(log_loop_sender_clone, &driver_package_ind_clone);
                        match command {
                            Ok(_) => {
                                println!("Status: Driver modify Successful");
                                log_status_loop_sender_clone.send_blocking(true).expect("The channel needs to be open.");
                            }
                            Err(_) => {
                                println!("Status: Driver modify Failed");
                                log_status_loop_sender_clone.send_blocking(false).expect("The channel needs to be open.");
                            }
                        }
                    });
                }));
                //
                drivers_list_row.append(&driver_expander_row);
            }
        }
    } else {
        let window_no_drivers_box_text = adw::StatusPage::builder()
        .icon_name("face-cool")
        .title(t!("window_no_drivers_box_text_title"))
        .description(t!("window_no_drivers_box_text_description"))
        .build();
        window_no_drivers_box_text.add_css_class("compact");

        window_box.append(&window_no_drivers_box_text);
    }

    main_window.remove(loading_box);
    main_window.append(&window_box);
}
