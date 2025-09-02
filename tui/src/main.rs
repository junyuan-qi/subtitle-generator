use std::io;
use std::io::BufRead;
use std::process::{Child, Command, Stdio};
use std::path::{Path, PathBuf};
use std::sync::mpsc::{self, Receiver};
use std::thread;
use std::time::Duration;

use anyhow::{Context, Result};
use crossterm::event::{self, DisableMouseCapture, EnableMouseCapture, Event, KeyCode, KeyEvent, KeyModifiers};
use crossterm::terminal::{disable_raw_mode, enable_raw_mode, EnterAlternateScreen, LeaveAlternateScreen};
use crossterm::execute;
use once_cell::sync::Lazy;
use ratatui::backend::CrosstermBackend;
use ratatui::layout::{Constraint, Direction, Layout, Rect};
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{List, ListItem, Paragraph, Wrap};
use portable_pty::{native_pty_system, CommandBuilder as PtyCommandBuilder, MasterPty, PtySize};
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
        ("No translation", "")
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
    Running,
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
    // running
    logs: Vec<String>,
    process_state: Option<ProcessState>,
    last_error: Option<String>,
}

enum LogMsg {
    Append(String),
    Replace(String),
}

struct ProcessState {
    child: Option<Child>,
    child_pty: Option<Box<dyn portable_pty::Child + Send>>, // when running via PTY
    master_pty: Option<Box<dyn MasterPty + Send>>,          // to allow resizing
    log_rx: Receiver<LogMsg>,
    finished: bool,
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
            logs: Vec::new(),
            process_state: None,
            last_error: None,
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
                if self.burn_in { Step::BurnUse } else { Step::Summary }
            }
            Step::BurnUse => Step::BurnFormat,
            Step::BurnFormat => Step::Summary,
            Step::Summary => Step::Running,
            Step::Running => Step::Running,
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
                if self.burn_in { Step::BurnFormat } else { Step::BurnIn }
            }
            Step::Running => Step::Summary,
        };
    }

    fn build_program_and_args(&self) -> (String, Vec<String>, Option<PathBuf>) {
        // Prefer `uv run subtitle-gen` if a pyproject is found upward from CWD.
        let py_dir = find_pyproject_dir();
        let program = if py_dir.is_some() { "uv".to_string() } else { "subtitle-gen".to_string() };
        let mut args = Vec::new();
        if py_dir.is_some() {
            args.push("run".into());
            args.push("subtitle-gen".into());
        }

        match self.mode {
            SourceMode::Local => {
                args.push("--src".into());
                args.push(self.src_path.clone());
            }
            SourceMode::YouTube => {
                if !self.yt_url.trim().is_empty() {
                    args.push("--yt".into());
                    args.push(self.yt_url.trim().into());
                }
                args.push("--src".into());
                args.push(self.src_path.clone());
            }
        }

        let (_label, code) = LANG_OPTIONS[self.lang_index];
        if !code.is_empty() {
            args.push("--lang".into());
            args.push(code.into());
        }

        if self.overwrite {
            args.push("--overwrite".into());
        }

        if self.burn_in {
            args.push("--burn-in".into());
            args.push("--burn-use".into());
            args.push(match self.burn_use {
                BurnUse::Translated => "translated",
                BurnUse::Original => "original",
            }.into());
            args.push("--burn-format".into());
            args.push(match self.burn_format {
                BurnFormat::Mp4 => "mp4",
                BurnFormat::Webm => "webm",
            }.into());
        }

        (program, args, py_dir)
    }
}

fn main() -> Result<()> {
    // Setup terminal
    enable_raw_mode()?;
    let mut stdout = io::stdout();
    execute!(stdout, EnterAlternateScreen, EnableMouseCapture)?;
    let backend = CrosstermBackend::new(stdout);
    let mut terminal = Terminal::new(backend)?;

    let res = run_app(&mut terminal);

    // Restore terminal
    disable_raw_mode().ok();
    execute!(
        terminal.backend_mut(),
        LeaveAlternateScreen,
        DisableMouseCapture
    )
    .ok();
    terminal.show_cursor().ok();

    res
}

fn run_app(terminal: &mut Terminal<CrosstermBackend<io::Stdout>>) -> Result<()> {
    let mut app = App::new();
    let tick_rate = Duration::from_millis(100);

    loop {
        terminal.draw(|f| ui(f, &mut app))?;

        // handle logs if running and check process status
        if let Some(ps) = app.process_state.as_mut() {
            for msg in ps.log_rx.try_iter() {
                match msg {
                    LogMsg::Append(line) => app.logs.push(line),
                    LogMsg::Replace(line) => {
                        if let Some(last) = app.logs.last_mut() {
                            *last = line;
                        } else {
                            app.logs.push(line);
                        }
                    }
                }
            }
            // Check piped child
            if let Some(child) = ps.child.as_mut() {
                if let Ok(Some(status)) = child.try_wait() {
                    ps.finished = true;
                    let code = status.code();
                    app.logs.push(format!("[done] process exited with {:?}", code));
                    ps.child = None;
                }
            }
            // Check PTY child
            if let Some(child) = ps.child_pty.as_mut() {
                if let Ok(Some(_status)) = child.try_wait() {
                    ps.finished = true;
                    app.logs.push("[done] process exited".to_string());
                    ps.child_pty = None;
                }
            }
        }

        if event::poll(tick_rate)? {
            match event::read()? {
                Event::Key(key) => {
                    if handle_key(&mut app, key)? { break; }
                }
                Event::Resize(cols, rows) => {
                    if let Some(ps) = app.process_state.as_mut() {
                        if let Some(master) = ps.master_pty.as_mut() {
                            let _ = master.resize(PtySize { rows, cols, pixel_width: 0, pixel_height: 0 });
                        }
                    }
                }
                _ => {}
            }
        }
    }

    Ok(())
}

fn handle_key(app: &mut App, key: KeyEvent) -> Result<bool> {
    // Global quit
    if key.code == KeyCode::Char('q') && key.modifiers.is_empty() {
        return Ok(true);
    }
    if key.code == KeyCode::Char('c') && key.modifiers.contains(KeyModifiers::CONTROL) {
        return Ok(true);
    }

    match app.step {
        Step::Mode => match key.code {
            KeyCode::Up | KeyCode::Char('k') => { app.mode = SourceMode::Local; }
            KeyCode::Down | KeyCode::Char('j') => { app.mode = SourceMode::YouTube; }
            KeyCode::Enter => { app.next_step(); }
            _ => {}
        },
        Step::SrcPath => match key.code {
            KeyCode::Enter => app.next_step(),
            KeyCode::Backspace => { app.src_path.pop(); }
            KeyCode::Char('/') | KeyCode::Char('.') | KeyCode::Char('-') | KeyCode::Char('_') => {
                app.src_path.push(match key.code { KeyCode::Char(c) => c, _ => unreachable!() });
            }
            KeyCode::Char(c) => {
                if !key.modifiers.contains(KeyModifiers::CONTROL) {
                    app.src_path.push(c);
                }
            }
            KeyCode::Tab => { app.next_step(); }
            KeyCode::Esc => { app.prev_step(); }
            _ => {}
        },
        Step::YtUrl => match key.code {
            KeyCode::Enter => app.next_step(),
            KeyCode::Backspace => { app.yt_url.pop(); }
            KeyCode::Char(c) => {
                if !key.modifiers.contains(KeyModifiers::CONTROL) {
                    app.yt_url.push(c);
                }
            }
            KeyCode::Tab => { app.next_step(); }
            KeyCode::Esc => { app.prev_step(); }
            _ => {}
        },
        Step::Lang => match key.code {
            KeyCode::Up | KeyCode::Char('k') => {
                if app.lang_index > 0 { app.lang_index -= 1; }
            }
            KeyCode::Down | KeyCode::Char('j') => {
                if app.lang_index + 1 < LANG_OPTIONS.len() { app.lang_index += 1; }
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
                // start run
                let (prog, argv, workdir) = app.build_program_and_args();
                match start_process(&prog, &argv, workdir.as_deref()) {
                    Ok(ps) => {
                        app.logs.clear();
                        app.process_state = Some(ps);
                        app.step = Step::Running;
                        app.last_error = None;
                    }
                    Err(e) => {
                        app.last_error = Some(format!("{}", e));
                    }
                }
            }
            KeyCode::Esc => app.prev_step(),
            _ => {}
        },
        Step::Running => match key.code {
            KeyCode::Char('c') => {
                if let Some(ps) = app.process_state.as_mut() {
                    if let Some(child) = ps.child.as_mut() {
                        let _ = child.kill();
                        app.logs.push("[canceled] Sent kill to process".into());
                    }
                    if let Some(child) = ps.child_pty.as_mut() {
                        let _ = child.kill();
                        app.logs.push("[canceled] Sent kill to process".into());
                    }
                }
            }
            KeyCode::Char('b') => {
                // back to summary if finished
                if let Some(ps) = app.process_state.as_ref() {
                    if ps.finished { app.step = Step::Summary; }
                } else {
                    app.step = Step::Summary;
                }
            }
            KeyCode::Esc => { app.step = Step::Summary; }
            _ => {}
        },
    }

    Ok(false)
}

fn start_process(program: &str, args: &[String], workdir: Option<&Path>) -> Result<ProcessState> {
    // Try PTY first to get live, styled output
    #[cfg(unix)]
    if let Ok(ps) = start_process_with_pty(program, args, workdir) {
        return Ok(ps);
    }

    let mut cmd = Command::new(program);
    for a in args { cmd.arg(a); }
    cmd.stdout(Stdio::piped()).stderr(Stdio::piped());
    if let Some(d) = workdir { cmd.current_dir(d); }
    // Encourage unbuffered, colored output when not a TTY
    cmd.env("PYTHONUNBUFFERED", "1");
    cmd.env("FORCE_COLOR", "1");

    let mut child = cmd.spawn().with_context(|| format!(
        "Failed to spawn process: {} {}",
        program,
        args.join(" ")
    ))?;

    let stdout = child.stdout.take();
    let stderr = child.stderr.take();

    let (tx, rx) = mpsc::channel::<LogMsg>();

    // Reader thread
    let mut readers = Vec::new();
    if let Some(out) = stdout {
        let tx_clone = tx.clone();
        readers.push(thread::spawn(move || {
            let mut reader = io::BufReader::new(out);
            let mut line = String::new();
            loop {
                line.clear();
                match reader.read_line(&mut line) {
                    Ok(0) => break,
                    Ok(_) => {
                        let _ = tx_clone.send(LogMsg::Append(line.trim_end_matches(['\n','\r']).to_string()));
                    }
                    Err(_) => break,
                }
            }
        }));
    }
    if let Some(err) = stderr {
        let tx_clone = tx.clone();
        readers.push(thread::spawn(move || {
            let mut reader = io::BufReader::new(err);
            let mut line = String::new();
            loop {
                line.clear();
                match reader.read_line(&mut line) {
                    Ok(0) => break,
                    Ok(_) => {
                        let _ = tx_clone.send(LogMsg::Append(line.trim_end_matches(['\n','\r']).to_string()));
                    }
                    Err(_) => break,
                }
            }
        }));
    }

    Ok(ProcessState { child: Some(child), child_pty: None, master_pty: None, log_rx: rx, finished: false })
}

fn ui(f: &mut ratatui::Frame, app: &mut App) {
    let size = f.size();
    let chunks = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Length(1),
            Constraint::Min(5),
        ])
        .split(size);

    // Header: single line, no border
    let header = Line::from(vec![
        Span::styled("subtitle-tui ", Style::default().fg(Color::Cyan).add_modifier(Modifier::BOLD)),
        Span::raw("— an interactive wizard to use subtitle generator"),
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
        Step::Running => render_running(f, chunks[1], app),
    }
}

fn render_mode(f: &mut ratatui::Frame, area: Rect, app: &App) {
    let mut lines = Vec::new();
    lines.push(q_line("How do you want to get videos?"));
    lines.push(Line::from(""));
    let opts = ["Use a local folder", "Download from YouTube"];
    let idx = match app.mode { SourceMode::Local => 0, SourceMode::YouTube => 1 };
    for (i, opt) in opts.iter().enumerate() {
        if i == idx {
            lines.push(Line::from(vec![Span::styled("› ", Style::default().fg(Color::Yellow)), Span::raw(*opt)]));
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
        Line::from("") ,
        Line::from(vec![Span::raw("Path: "), Span::styled(&app.src_path, Style::default().fg(Color::Green))]),
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
        Line::from(vec![Span::raw("URL: "), Span::styled(&app.yt_url, Style::default().fg(Color::Green))]),
    ];
    lines.push(Line::from(""));
    lines.push(hint_line(app));
    f.render_widget(Paragraph::new(lines).wrap(Wrap { trim: false }), area);
}

fn render_lang(f: &mut ratatui::Frame, area: Rect, app: &App) {
    let lines_top = vec![q_line("What’s the target language?"), Line::from("")];
    f.render_widget(Paragraph::new(lines_top), Rect { x: area.x, y: area.y, width: area.width, height: 2 });
    // leave 2 lines at bottom for blank + hint
    let list_area = Rect { x: area.x, y: area.y + 2, width: area.width, height: area.height.saturating_sub(4) };
    let items: Vec<ListItem> = LANG_OPTIONS
        .iter()
        .enumerate()
        .map(|(i, (label, code))| {
            let line = Line::from(format!("{} ({})", label, code));
            let mut item = ListItem::new(line);
            if i == app.lang_index { item = item.style(Style::default().fg(Color::Yellow)); }
            item
        })
        .collect();

    let list = List::new(items).highlight_style(Style::default().add_modifier(Modifier::BOLD));
    f.render_widget(list, list_area);
    // render hint at bottom of area
    let hint_area = Rect { x: area.x, y: area.bottom().saturating_sub(2), width: area.width, height: 2 };
    f.render_widget(Paragraph::new(vec![Line::from(""), hint_line(app)]).wrap(Wrap { trim: true }), hint_area);
}

fn find_pyproject_dir() -> Option<PathBuf> {
    let mut cur = std::env::current_dir().ok()?;
    for _ in 0..5 {
        let candidate = cur.join("pyproject.toml");
        if candidate.exists() {
            return Some(cur);
        }
        if !cur.pop() { break; }
    }
    None
}

fn render_overwrite(f: &mut ratatui::Frame, area: Rect, app: &App) {
    let text = if app.overwrite { "Yes" } else { "No" };
    let mut lines = vec![
        q_line("Overwrite existing results if found?"),
        Line::from(""),
        Line::from(Span::styled(text, Style::default().fg(if app.overwrite { Color::Green } else { Color::Gray }))),
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
        Line::from(Span::styled(text, Style::default().fg(if app.burn_in { Color::Green } else { Color::Gray }))),
    ];
    lines.push(Line::from(""));
    lines.push(hint_line(app));
    f.render_widget(Paragraph::new(lines), area);
}

fn render_burn_use(f: &mut ratatui::Frame, area: Rect, app: &App) {
    let mut lines = vec![q_line("Which subtitles should be burned?"), Line::from("")];
    let opts = ["Translated", "Original"];
    let idx = match app.burn_use { BurnUse::Translated => 0, BurnUse::Original => 1 };
    for (i, opt) in opts.iter().enumerate() {
        if i == idx {
            lines.push(Line::from(vec![Span::styled("› ", Style::default().fg(Color::Yellow)), Span::raw(*opt)]));
        } else {
            lines.push(Line::from(format!("  {}", opt)));
        }
    }
    lines.push(Line::from(""));
    lines.push(hint_line(app));
    f.render_widget(Paragraph::new(lines), area);
}

fn render_burn_format(f: &mut ratatui::Frame, area: Rect, app: &App) {
    let mut lines = vec![q_line("What output format for burned video?"), Line::from("")];
    let opts = ["MP4", "WebM"];
    let idx = match app.burn_format { BurnFormat::Mp4 => 0, BurnFormat::Webm => 1 };
    for (i, opt) in opts.iter().enumerate() {
        if i == idx {
            lines.push(Line::from(vec![Span::styled("› ", Style::default().fg(Color::Yellow)), Span::raw(*opt)]));
        } else {
            lines.push(Line::from(format!("  {}", opt)));
        }
    }
    lines.push(Line::from(""));
    lines.push(hint_line(app));
    f.render_widget(Paragraph::new(lines), area);
}

fn render_summary(f: &mut ratatui::Frame, area: Rect, app: &App) {
    let (prog, args, _wd) = app.build_program_and_args();
    let preview = format!("{} {}", prog, args.join(" "));
    let mut lines = vec![
        Line::from(Span::styled("Summary", Style::default().add_modifier(Modifier::BOLD))),
        Line::from(""),
        Line::from("I will run:"),
        Line::from(preview.clone()),
    ];
    if let Some(err) = &app.last_error {
        lines.push(Line::from(Span::styled(err, Style::default().fg(Color::Red))));
    }
    lines.push(Line::from(""));
    lines.push(hint_line(app));
    f.render_widget(Paragraph::new(lines).wrap(Wrap { trim: false }), area);
}

fn render_running(f: &mut ratatui::Frame, area: Rect, app: &mut App) {
    let top = Paragraph::new(Line::from(Span::styled("Running", Style::default().add_modifier(Modifier::BOLD))));
    f.render_widget(top, Rect { x: area.x, y: area.y, width: area.width, height: 1 });
    // Use all remaining lines for logs (no hint/footer reserved)
    let logs_area = Rect { x: area.x, y: area.y + 1, width: area.width, height: area.height.saturating_sub(1) };
    // Keep PTY width/height exactly synced to the logs area to avoid padding/misalignment
    if let Some(ps) = app.process_state.as_mut() {
        if let Some(master) = ps.master_pty.as_mut() {
            let _ = master.resize(PtySize { rows: logs_area.height, cols: logs_area.width, pixel_width: 0, pixel_height: 0 });
        }
    }
    // Show the tail of logs so progress appears live without manual scrolling
    let capacity = logs_area.height as usize;
    let total = app.logs.len();
    let start = total.saturating_sub(capacity);
    let tail: Vec<Line> = app.logs[start..]
        .iter()
        .map(|l| Line::from(l.as_str()))
        .collect();
    let para = Paragraph::new(tail);
    f.render_widget(para, logs_area);
    // No hint while running to avoid truncating output
}

#[cfg(unix)]
fn start_process_with_pty(program: &str, args: &[String], workdir: Option<&Path>) -> Result<ProcessState> {
    use std::io::Read;
    let pty_system = native_pty_system();
    let (term_cols, term_rows) = crossterm::terminal::size().unwrap_or((120, 40));
    let pair = pty_system
        .openpty(PtySize { rows: term_rows, cols: term_cols, pixel_width: 0, pixel_height: 0 })
        .context("open pty")?;

    let mut cmd = PtyCommandBuilder::new(program);
    for a in args { cmd.arg(a); }
    if let Some(d) = workdir { cmd.cwd(d); }
    cmd.env("PYTHONUNBUFFERED", "1");
    cmd.env("FORCE_COLOR", "1");

    let child = pair
        .slave
        .spawn_command(cmd)
        .context("spawn pty child")?;
    drop(pair.slave);

    let mut reader = pair.master.try_clone_reader().context("clone pty reader")?;
    let master_for_state = pair.master;

    let (tx, rx) = mpsc::channel::<LogMsg>();
    thread::spawn(move || {
        let mut buf = [0u8; 8192];
        let mut acc = String::new();
        loop {
            match reader.read(&mut buf) {
                Ok(0) => {
                    if !acc.is_empty() {
                        let _ = tx.send(LogMsg::Append(acc.clone()));
                        acc.clear();
                    }
                    break;
                }
                Ok(n) => {
                    let chunk = String::from_utf8_lossy(&buf[..n]);
                    for c in chunk.chars() {
                        match c {
                            '\r' => {
                                // Emit a replace for progress updates
                                if !acc.is_empty() {
                                    let _ = tx.send(LogMsg::Replace(acc.clone()));
                                    acc.clear();
                                }
                            }
                            '\n' => {
                                let line = std::mem::take(&mut acc);
                                let _ = tx.send(LogMsg::Append(line));
                            }
                            _ => acc.push(c),
                        }
                    }
                }
                Err(_) => break,
            }
        }
    });

    Ok(ProcessState { child: None, child_pty: Some(child), master_pty: Some(master_for_state), log_rx: rx, finished: false })
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
        Step::BurnFormat => "Left/Right to choose. Enter to continue. Esc to go back. Ctrl+C to quit.",
        Step::Summary => "Press Enter to run. Esc to go back. Ctrl+C to quit.",
        Step::Running => "Esc to back. Ctrl+C to quit.",
    };
    Line::from(Span::styled(text, Style::default().fg(Color::DarkGray)))
}

fn q_line(text: &str) -> Line<'static> {
    Line::from(vec![
        Span::styled("? ", Style::default().fg(Color::Green).add_modifier(Modifier::BOLD)),
        Span::styled(text.to_string(), Style::default().add_modifier(Modifier::BOLD)),
    ])
}
