use serde::{Deserialize, Deserializer, Serialize};
fn present_epoch<'de, D: Deserializer<'de>>(d: D) -> Result<Option<u64>, D::Error> {
    u64::deserialize(d).map(Some)
}
#[derive(Clone, Copy, Deserialize, Serialize, PartialEq, Eq, PartialOrd, Ord)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum Kind {
    Type,
    Method,
    Function,
    Field,
    Module,
    File,
    Variable,
}
impl Kind {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Type => "TYPE",
            Self::Method => "METHOD",
            Self::Function => "FUNCTION",
            Self::Field => "FIELD",
            Self::Module => "MODULE",
            Self::File => "FILE",
            Self::Variable => "VARIABLE",
        }
    }
}
#[derive(Clone, Copy, Deserialize, Serialize, PartialEq, Eq, PartialOrd, Ord)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum Relation {
    Calls,
    References,
    Implements,
    Overrides,
    Contains,
}
impl Relation {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Calls => "CALLS",
            Self::References => "REFERENCES",
            Self::Implements => "IMPLEMENTS",
            Self::Overrides => "OVERRIDES",
            Self::Contains => "CONTAINS",
        }
    }
}
#[derive(Clone, Copy, Deserialize, Serialize, PartialEq, Eq, PartialOrd, Ord)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum Quality {
    Lexical,
    Probable,
    Derived,
    Exact,
    Verified,
}
impl Quality {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Lexical => "LEXICAL",
            Self::Probable => "PROBABLE",
            Self::Derived => "DERIVED",
            Self::Exact => "EXACT",
            Self::Verified => "VERIFIED",
        }
    }
}
#[derive(Clone, Copy, Deserialize, Serialize, PartialEq, Eq, PartialOrd, Ord)]
#[serde(rename_all = "SCREAMING_SNAKE_CASE")]
pub enum Polarity {
    Positive,
    Negative,
}
impl Polarity {
    pub fn as_str(self) -> &'static str {
        match self {
            Self::Positive => "POSITIVE",
            Self::Negative => "NEGATIVE",
        }
    }
}
#[derive(Clone, Copy, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Entity {
    pub id: u64,
    pub kind: Kind,
    pub name_sid: u32,
    pub container: Option<u64>,
}
#[derive(Clone, Copy, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Edge {
    pub subject: u64,
    pub relation: Relation,
    pub object: u64,
}
#[derive(Clone, Copy, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Evidence {
    pub freshness_epoch: u64,
    pub lineage: u32,
    pub object: u64,
    pub polarity: Polarity,
    pub proposition: u64,
    pub quality: Quality,
    pub relation: Relation,
    pub subject: u64,
}
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Fixture {
    pub schema: String,
    pub snapshot: String,
    /// `None` when the fixture has no `epoch`; a present value must be an integer
    /// (`null` is still rejected, as it was with a plain `u64`).
    #[serde(default, rename = "epoch", deserialize_with = "present_epoch")]
    pub _epoch: Option<u64>,
    pub strings: Vec<String>,
    pub entities: Vec<Entity>,
    pub relations: Vec<Edge>,
    pub evidence: Vec<Evidence>,
    #[serde(default)]
    pub complete: bool,
}
