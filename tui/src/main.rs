use std::io;
use std::path::PathBuf;
use std::time::Duration;

use anyhow::Result;
use crossterm::event::{
    self, DisableMouseCapture, EnableMouseCapture, Event, KeyCode, KeyEvent, KeyModifiers,
};
use crossterm::execute;
use crossterm::terminal::{
    disable_raw_mode, enable_raw_mode, EnterAlternateScreen, LeaveAlternateScreen,
};
use once_cell::sync::Lazy;
use ratatui::backend::CrosstermBackend;
use ratatui::layout::{Constraint, Direction, Layout, Rect};
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{List, ListItem, Paragraph, Wrap};
use ratatui::Terminal;

static LANG_OPTIONS: Lazy<Vec<(&str, &str)>> = Lazy::new(|| {
    vec![
        ("Chinese", "zh"),
        ("English", "en"),
        ("Japanese", "ja"),
        ("Korean", "ko"),
        ("Spanish", "es"),
        ("French", "fr"),
        ("German", "de"),
        ("Portuguese", "pt"),
        ("No translation", ""),
    ]
});

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum SourceMode {
    Local,
    YouTube,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum BurnUse {
    Translated,
    Original,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum BurnFormat {
    Mp4,
    Webm,
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Step {
    Mode,
    SrcPath,
    YtUrl,
    Lang,
    Overwrite,
    BurnIn,
    BurnUse,
    BurnFormat,
    Summary,
}

struct App {
    step: Step,
    mode: SourceMode,
    src_path: String,
    yt_url: String,
    lang_index: usize,
    overwrite: bool,
    burn_in: bool,
    burn_use: BurnUse,
    burn_format: BurnFormat,
    should_exit: bool,
    generated_command: Option<String>,
}

impl App {
    fn new() -> Self {
        Self {
            step: Step::Mode,
            mode: SourceMode::Local,
            src_path: String::from("videos"),
            yt_url: String::new(),
            lang_index: 0, // Chinese by default
            overwrite: false,
            burn_in: false,
            burn_use: BurnUse::Translated,
            burn_format: BurnFormat::Mp4,
            should_exit: false,
            generated_command: None,
        }
    }

    fn next_step(&mut self) {
        self.step = match self.step {
            Step::Mode => Step::SrcPath,
            Step::SrcPath => match self.mode {
                SourceMode::Local => Step::Lang,
                SourceMode::YouTube => Step::YtUrl,
            },
            Step::YtUrl => Step::Lang,
            Step::Lang => Step::Overwrite,
            Step::Overwrite => Step::BurnIn,
            Step::BurnIn => {
                if self.burn_in {
                    Step::BurnUse
                } else {
                    Step::Summary
                }
            }
            Step::BurnUse => Step::BurnFormat,
            Step::BurnFormat => Step::Summary,
            Step::Summary => Step::Summary,
        };
    }

    fn prev_step(&mut self) {
        self.step = match self.step {
            Step::Mode => Step::Mode,
            Step::SrcPath => Step::Mode,
            Step::YtUrl => Step::SrcPath,
            Step::Lang => match self.mode {
                SourceMode::Local => Step::SrcPath,
                SourceMode::YouTube => Step::YtUrl,
            },
            Step::Overwrite => Step::Lang,
            Step::BurnIn => Step::Overwrite,
            Step::BurnUse => Step::BurnIn,
            Step::BurnFormat => Step::BurnUse,
            Step::Summary => {
                if self.burn_in {
                    Step::BurnFormat
                } else {
                    Step::BurnIn
                }
            }
        };
    }

    fn build_command(&self) -> String {
        // Prefer `uv run subtitle-gen` if a pyproject is found upward from CWD.
        let py_dir = find_pyproject_dir();
        let program = if py_dir.is_some() {
            "uv"
        } else {
            "subtitle-gen"
        };
        let mut parts = vec![program.to_string()];

        if py_dir.is_some() {
            parts.push("run".to_string());
            parts.push("subtitle-gen".to_string());
        }

        match self.mode {
            SourceMode::Local => {
                parts.push("--src".to_string());
                parts.push(self.src_path.clone());
            }
            SourceMode::YouTube => {
                if !self.yt_url.trim().is_empty() {
                    parts.push("--yt".to_string());
                    parts.push(format!("\"{}\"", self.yt_url.trim()));
                }
                parts.push("--src".to_string());
                parts.push(self.src_path.clone());
            }
        }

        let (_label, code) = LANG_OPTIONS[self.lang_index];
        if !code.is_empty() {
            parts.push("--lang".to_string());
            parts.push(code.to_string());
        }

        if self.overwrite {
            parts.push("--overwrite".to_string());
        }

        if self.burn_in {
            parts.push("--burn-in".to_string());
            parts.push("--burn-use".to_string());
            parts.push(
                match self.burn_use {
                    BurnUse::Translated => "translated",
                    BurnUse::Original => "original",
                }
                .to_string(),
            );
            parts.push("--burn-format".to_string());
            parts.push(
                match self.burn_format {
                    BurnFormat::Mp4 => "mp4",
                    BurnFormat::Webm => "webm",
                }
                .to_string(),
            );
        }

        parts.join(" ")
    }

    fn execute(&mut self) {
        self.generated_command = Some(self.build_command());
        self.should_exit = true;
    }
}

fn main() -> Result<()> {
    // Setup terminal
    enable_raw_mode()?;
    let mut stdout = io::stdout();
    execute!(stdout, EnterAlternateScreen, EnableMouseCapture)?;
    let backend = CrosstermBackend::new(stdout);
    let mut terminal = Terminal::new(backend)?;

    let result = run_app(&mut terminal);

    // Restore terminal
    disable_raw_mode().ok();
    execute!(
        terminal.backend_mut(),
        LeaveAlternateScreen,
        DisableMouseCapture
    )
    .ok();
    terminal.show_cursor().ok();

    result
}

fn run_app(terminal: &mut Terminal<CrosstermBackend<io::Stdout>>) -> Result<()> {
    let mut app = App::new();
    let tick_rate = Duration::from_millis(100);

    loop {
        terminal.draw(|f| ui(f, &mut app))?;

        if app.should_exit {
            break;
        }

        if event::poll(tick_rate)? {
            if let Event::Key(key) = event::read()? {
                handle_key(&mut app, key)?;
            }
        }
    }

    // Simply display the generated command for manual execution
    if let Some(cmd) = app.generated_command {
        println!();
        println!("🎬 Generated subtitle generator command:");
        println!();
        println!("   {}", cmd);
        println!();

        // Also create a convenience script
        if let Some(py_dir) = find_pyproject_dir() {
            let script_path = py_dir.join("run_generated.sh");
            let script_content = format!("#!/bin/bash\ncd \"{}\"\n{}\n", py_dir.display(), cmd);

            if std::fs::write(&script_path, script_content).is_ok() {
                // Make executable on Unix
                #[cfg(unix)]
                {
                    use std::os::unix::fs::PermissionsExt;
                    if let Ok(mut perms) = std::fs::metadata(&script_path).map(|m| m.permissions())
                    {
                        perms.set_mode(0o755);
                        let _ = std::fs::set_permissions(&script_path, perms);
                    }
                }

                println!("📝 Also saved as: {}", script_path.display());
                println!("   Run with: ./run_generated.sh");
                println!();
            }
        }

        println!("📋 Copy and paste the command above to run subtitle generation");
        if find_pyproject_dir().is_some() {
            println!("   (Run from the project root directory)");
        }
        println!();
    }

    Ok(())
}

fn handle_key(app: &mut App, key: KeyEvent) -> Result<()> {
    // Global quit
    if key.code == KeyCode::Char('q') && key.modifiers.is_empty() {
        app.should_exit = true;
        return Ok(());
    }
    if key.code == KeyCode::Char('c') && key.modifiers.contains(KeyModifiers::CONTROL) {
        app.should_exit = true;
        return Ok(());
    }

    match app.step {
        Step::Mode => match key.code {
            KeyCode::Up | KeyCode::Char('k') => {
                app.mode = SourceMode::Local;
            }
            KeyCode::Down | KeyCode::Char('j') => {
                app.mode = SourceMode::YouTube;
            }
            KeyCode::Enter => {
                app.next_step();
            }
            _ => {}
        },
        Step::SrcPath => match key.code {
            KeyCode::Enter => app.next_step(),
            KeyCode::Backspace => {
                app.src_path.pop();
            }
            KeyCode::Char('/') | KeyCode::Char('.') | KeyCode::Char('-') | KeyCode::Char('_') => {
                app.src_path.push(match key.code {
                    KeyCode::Char(c) => c,
                    _ => unreachable!(),
                });
            }
            KeyCode::Char(c) => {
                if !key.modifiers.contains(KeyModifiers::CONTROL) {
                    app.src_path.push(c);
                }
            }
            KeyCode::Tab => {
                app.next_step();
            }
            KeyCode::Esc => {
                app.prev_step();
            }
            _ => {}
        },
        Step::YtUrl => match key.code {
            KeyCode::Enter => app.next_step(),
            KeyCode::Backspace => {
                app.yt_url.pop();
            }
            KeyCode::Char(c) => {
                if !key.modifiers.contains(KeyModifiers::CONTROL) {
                    app.yt_url.push(c);
                }
            }
            KeyCode::Tab => {
                app.next_step();
            }
            KeyCode::Esc => {
                app.prev_step();
            }
            _ => {}
        },
        Step::Lang => match key.code {
            KeyCode::Up | KeyCode::Char('k') => {
                if app.lang_index > 0 {
                    app.lang_index -= 1;
                }
            }
            KeyCode::Down | KeyCode::Char('j') => {
                if app.lang_index + 1 < LANG_OPTIONS.len() {
                    app.lang_index += 1;
                }
            }
            KeyCode::Enter | KeyCode::Tab => app.next_step(),
            KeyCode::Esc => app.prev_step(),
            _ => {}
        },
        Step::Overwrite => match key.code {
            KeyCode::Char(' ') => app.overwrite = !app.overwrite,
            KeyCode::Enter | KeyCode::Tab => app.next_step(),
            KeyCode::Esc => app.prev_step(),
            _ => {}
        },
        Step::BurnIn => match key.code {
            KeyCode::Char(' ') => app.burn_in = !app.burn_in,
            KeyCode::Enter | KeyCode::Tab => app.next_step(),
            KeyCode::Esc => app.prev_step(),
            _ => {}
        },
        Step::BurnUse => match key.code {
            KeyCode::Left | KeyCode::Char('h') => app.burn_use = BurnUse::Translated,
            KeyCode::Right | KeyCode::Char('l') => app.burn_use = BurnUse::Original,
            KeyCode::Enter | KeyCode::Tab => app.next_step(),
            KeyCode::Esc => app.prev_step(),
            _ => {}
        },
        Step::BurnFormat => match key.code {
            KeyCode::Left | KeyCode::Char('h') => app.burn_format = BurnFormat::Mp4,
            KeyCode::Right | KeyCode::Char('l') => app.burn_format = BurnFormat::Webm,
            KeyCode::Enter | KeyCode::Tab => app.next_step(),
            KeyCode::Esc => app.prev_step(),
            _ => {}
        },
        Step::Summary => match key.code {
            KeyCode::Char('r') | KeyCode::Enter => {
                app.execute();
            }
            KeyCode::Esc => app.prev_step(),
            _ => {}
        },
    }

    Ok(())
}

fn ui(f: &mut ratatui::Frame, app: &mut App) {
    let size = f.size();
    let chunks = Layout::default()
        .direction(Direction::Vertical)
        .constraints([Constraint::Length(1), Constraint::Min(5)])
        .split(size);

    // Header: single line, no border
    let header = Line::from(vec![
        Span::styled(
            "subtitle-tui ",
            Style::default()
                .fg(Color::Cyan)
                .add_modifier(Modifier::BOLD),
        ),
        Span::raw("— an interactive wizard for subtitle generator"),
    ]);
    f.render_widget(Paragraph::new(header), chunks[0]);

    match app.step {
        Step::Mode => render_mode(f, chunks[1], app),
        Step::SrcPath => render_src_path(f, chunks[1], app),
        Step::YtUrl => render_yt_url(f, chunks[1], app),
        Step::Lang => render_lang(f, chunks[1], app),
        Step::Overwrite => render_overwrite(f, chunks[1], app),
        Step::BurnIn => render_burnin(f, chunks[1], app),
        Step::BurnUse => render_burn_use(f, chunks[1], app),
        Step::BurnFormat => render_burn_format(f, chunks[1], app),
        Step::Summary => render_summary(f, chunks[1], app),
    }
}

fn render_mode(f: &mut ratatui::Frame, area: Rect, app: &App) {
    let mut lines = Vec::new();
    lines.push(q_line("How do you want to get videos?"));
    lines.push(Line::from(""));
    let opts = ["Use a local folder", "Download from YouTube"];
    let idx = match app.mode {
        SourceMode::Local => 0,
        SourceMode::YouTube => 1,
    };
    for (i, opt) in opts.iter().enumerate() {
        if i == idx {
            lines.push(Line::from(vec![
                Span::styled("› ", Style::default().fg(Color::Yellow)),
                Span::raw(*opt),
            ]));
        } else {
            lines.push(Line::from(format!("  {}", opt)));
        }
    }
    lines.push(Line::from(""));
    lines.push(hint_line(app));
    f.render_widget(Paragraph::new(lines).wrap(Wrap { trim: true }), area);
}

fn render_src_path(f: &mut ratatui::Frame, area: Rect, app: &App) {
    let title = match app.mode {
        SourceMode::Local => "Where are the videos located?",
        SourceMode::YouTube => "Where should downloaded videos be saved?",
    };
    let lines = vec![
        q_line(title),
        Line::from(""),
        Line::from(vec![
            Span::raw("Path: "),
            Span::styled(&app.src_path, Style::default().fg(Color::Green)),
        ]),
    ];
    let mut lines = lines;
    lines.push(Line::from(""));
    lines.push(hint_line(app));
    f.render_widget(Paragraph::new(lines).wrap(Wrap { trim: false }), area);
}

fn render_yt_url(f: &mut ratatui::Frame, area: Rect, app: &App) {
    let mut lines = vec![
        q_line("Paste a YouTube URL"),
        Line::from(""),
        Line::from(vec![
            Span::raw("URL: "),
            Span::styled(&app.yt_url, Style::default().fg(Color::Green)),
        ]),
    ];
    lines.push(Line::from(""));
    lines.push(hint_line(app));
    f.render_widget(Paragraph::new(lines).wrap(Wrap { trim: false }), area);
}

fn render_lang(f: &mut ratatui::Frame, area: Rect, app: &App) {
    let lines_top = vec![q_line("What's the target language?"), Line::from("")];
    f.render_widget(
        Paragraph::new(lines_top),
        Rect {
            x: area.x,
            y: area.y,
            width: area.width,
            height: 2,
        },
    );
    // leave 2 lines at bottom for blank + hint
    let list_area = Rect {
        x: area.x,
        y: area.y + 2,
        width: area.width,
        height: area.height.saturating_sub(4),
    };
    let items: Vec<ListItem> = LANG_OPTIONS
        .iter()
        .enumerate()
        .map(|(i, (label, code))| {
            let line = Line::from(format!("{} ({})", label, code));
            let mut item = ListItem::new(line);
            if i == app.lang_index {
                item = item.style(Style::default().fg(Color::Yellow));
            }
            item
        })
        .collect();

    let list = List::new(items).highlight_style(Style::default().add_modifier(Modifier::BOLD));
    f.render_widget(list, list_area);
    // render hint at bottom of area
    let hint_area = Rect {
        x: area.x,
        y: area.bottom().saturating_sub(2),
        width: area.width,
        height: 2,
    };
    f.render_widget(
        Paragraph::new(vec![Line::from(""), hint_line(app)]).wrap(Wrap { trim: true }),
        hint_area,
    );
}

fn find_pyproject_dir() -> Option<PathBuf> {
    let mut cur = std::env::current_dir().ok()?;
    for _ in 0..5 {
        let candidate = cur.join("pyproject.toml");
        if candidate.exists() {
            return Some(cur);
        }
        if !cur.pop() {
            break;
        }
    }
    None
}

fn render_overwrite(f: &mut ratatui::Frame, area: Rect, app: &App) {
    let text = if app.overwrite { "Yes" } else { "No" };
    let mut lines = vec![
        q_line("Overwrite existing results if found?"),
        Line::from(""),
        Line::from(Span::styled(
            text,
            Style::default().fg(if app.overwrite {
                Color::Green
            } else {
                Color::Gray
            }),
        )),
    ];
    lines.push(Line::from(""));
    lines.push(hint_line(app));
    f.render_widget(Paragraph::new(lines), area);
}

fn render_burnin(f: &mut ratatui::Frame, area: Rect, app: &App) {
    let text = if app.burn_in { "Yes" } else { "No" };
    let mut lines = vec![
        q_line("Do you want to burn subtitles into the video?"),
        Line::from(""),
        Line::from(Span::styled(
            text,
            Style::default().fg(if app.burn_in {
                Color::Green
            } else {
                Color::Gray
            }),
        )),
    ];
    lines.push(Line::from(""));
    lines.push(hint_line(app));
    f.render_widget(Paragraph::new(lines), area);
}

fn render_burn_use(f: &mut ratatui::Frame, area: Rect, app: &App) {
    let mut lines = vec![q_line("Which subtitles should be burned?"), Line::from("")];
    let opts = ["Translated", "Original"];
    let idx = match app.burn_use {
        BurnUse::Translated => 0,
        BurnUse::Original => 1,
    };
    for (i, opt) in opts.iter().enumerate() {
        if i == idx {
            lines.push(Line::from(vec![
                Span::styled("› ", Style::default().fg(Color::Yellow)),
                Span::raw(*opt),
            ]));
        } else {
            lines.push(Line::from(format!("  {}", opt)));
        }
    }
    lines.push(Line::from(""));
    lines.push(hint_line(app));
    f.render_widget(Paragraph::new(lines), area);
}

fn render_burn_format(f: &mut ratatui::Frame, area: Rect, app: &App) {
    let mut lines = vec![
        q_line("What output format for burned video?"),
        Line::from(""),
    ];
    let opts = ["MP4", "WebM"];
    let idx = match app.burn_format {
        BurnFormat::Mp4 => 0,
        BurnFormat::Webm => 1,
    };
    for (i, opt) in opts.iter().enumerate() {
        if i == idx {
            lines.push(Line::from(vec![
                Span::styled("› ", Style::default().fg(Color::Yellow)),
                Span::raw(*opt),
            ]));
        } else {
            lines.push(Line::from(format!("  {}", opt)));
        }
    }
    lines.push(Line::from(""));
    lines.push(hint_line(app));
    f.render_widget(Paragraph::new(lines), area);
}

fn render_summary(f: &mut ratatui::Frame, area: Rect, app: &App) {
    let command = app.build_command();
    let mut lines = vec![
        Line::from(Span::styled(
            "Summary",
            Style::default().add_modifier(Modifier::BOLD),
        )),
        Line::from(""),
        Line::from("Generated command:"),
        Line::from(""),
        Line::from(Span::styled(command, Style::default().fg(Color::Cyan))),
        Line::from(""),
        Line::from("Press Enter to generate command and exit"),
    ];
    lines.push(Line::from(""));
    lines.push(hint_line(app));
    f.render_widget(Paragraph::new(lines).wrap(Wrap { trim: false }), area);
}

fn hint_line(app: &App) -> Line<'static> {
    let text = match app.step {
        Step::Mode => "Use Up/Down to choose. Enter to continue. Ctrl+C to quit.",
        Step::SrcPath => "Type to edit. Enter to continue. Esc to go back. Ctrl+C to quit.",
        Step::YtUrl => "Enter to continue. Esc to go back. Ctrl+C to quit.",
        Step::Lang => "Use Up/Down to select. Enter to continue. Esc to go back. Ctrl+C to quit.",
        Step::Overwrite => "Space to toggle. Enter to continue. Esc to go back. Ctrl+C to quit.",
        Step::BurnIn => "Space to toggle. Enter to continue. Esc to go back. Ctrl+C to quit.",
        Step::BurnUse => "Left/Right to choose. Enter to continue. Esc to go back. Ctrl+C to quit.",
        Step::BurnFormat => {
            "Left/Right to choose. Enter to continue. Esc to go back. Ctrl+C to quit."
        }
        Step::Summary => "Press Enter to generate and exit. Esc to go back. Ctrl+C to quit.",
    };
    Line::from(Span::styled(text, Style::default().fg(Color::DarkGray)))
}

fn q_line(text: &str) -> Line<'static> {
    Line::from(vec![
        Span::styled(
            "? ",
            Style::default()
                .fg(Color::Green)
                .add_modifier(Modifier::BOLD),
        ),
        Span::styled(
            text.to_string(),
            Style::default().add_modifier(Modifier::BOLD),
        ),
    ])
}
