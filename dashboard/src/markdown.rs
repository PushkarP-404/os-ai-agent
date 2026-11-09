// ============================================================
// markdown.rs — pulldown-cmark → structured blocks
// ============================================================
use pulldown_cmark::{Event, Options, Parser, Tag};

#[derive(Debug, Clone)]
#[allow(dead_code)]
pub enum MdBlock {
    Text(String),
    Code { lang: String, body: String },
    Heading { level: u32, text: String },
    HRule,
}

/// Parse a markdown string into structured blocks for the UI
#[allow(dead_code)]
pub fn parse(input: &str) -> Vec<MdBlock> {
    let opts = Options::ENABLE_STRIKETHROUGH
        | Options::ENABLE_TASKLISTS
        | Options::ENABLE_TABLES;

    let parser = Parser::new_ext(input, opts);
    let mut blocks: Vec<MdBlock> = Vec::new();
    let mut current_text = String::new();
    let mut in_code_block = false;
    let mut code_lang = String::new();
    let mut code_body = String::new();
    let mut heading_level: u32 = 0;
    let mut in_heading = false;

    for event in parser {
        match event {
            Event::Start(Tag::Heading { level, .. }) => {
                flush_text(&mut current_text, &mut blocks);
                heading_level = level as u32;
                in_heading = true;
            }
            Event::End(pulldown_cmark::TagEnd::Heading(_)) => {
                if in_heading {
                    blocks.push(MdBlock::Heading {
                        level: heading_level,
                        text: std::mem::take(&mut current_text),
                    });
                    in_heading = false;
                }
            }
            Event::Start(Tag::CodeBlock(kind)) => {
                flush_text(&mut current_text, &mut blocks);
                in_code_block = true;
                code_lang = match kind {
                    pulldown_cmark::CodeBlockKind::Fenced(lang) => lang.to_string(),
                    _ => String::new(),
                };
                code_body.clear();
            }
            Event::End(pulldown_cmark::TagEnd::CodeBlock) => {
                blocks.push(MdBlock::Code {
                    lang: std::mem::take(&mut code_lang),
                    body: std::mem::take(&mut code_body),
                });
                in_code_block = false;
            }
            Event::Rule => {
                flush_text(&mut current_text, &mut blocks);
                blocks.push(MdBlock::HRule);
            }
            Event::Text(t) | Event::Code(t) => {
                if in_code_block {
                    code_body.push_str(&t);
                } else {
                    current_text.push_str(&t);
                }
            }
            Event::SoftBreak | Event::HardBreak => {
                current_text.push('\n');
            }
            _ => {}
        }
    }

    flush_text(&mut current_text, &mut blocks);
    blocks
}

#[allow(dead_code)]
fn flush_text(buf: &mut String, blocks: &mut Vec<MdBlock>) {
    let trimmed = buf.trim().to_string();
    if !trimmed.is_empty() {
        blocks.push(MdBlock::Text(trimmed));
    }
    buf.clear();
}

/// Flatten blocks back to plain text (for bubble content property)
#[allow(dead_code)]
pub fn to_plain(blocks: &[MdBlock]) -> String {
    blocks.iter().map(|b| match b {
        MdBlock::Text(t)             => t.clone(),
        MdBlock::Code { body, .. }   => format!("```\n{}\n```", body),
        MdBlock::Heading { text, .. } => text.clone(),
        MdBlock::HRule               => "---".to_string(),
    }).collect::<Vec<_>>().join("\n")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_code_block() {
        let md = "Hello\n\n```rust\nfn main() {}\n```\n\nWorld";
        let blocks = parse(md);
        assert!(blocks.iter().any(|b| matches!(b, MdBlock::Code { lang, .. } if lang == "rust")));
    }

    #[test]
    fn parses_heading() {
        let md = "# Title\nBody text";
        let blocks = parse(md);
        assert!(blocks.iter().any(|b| matches!(b, MdBlock::Heading { level: 1, .. })));
    }
}

