use std::cmp::min;
use crate::drain::darin_tree::DrainTree;
use crate::drain::drain_template::DrainTemplate;
use crate::id_generator::IdGenerator;
use std::collections::{HashMap, HashSet};
use std::str::FromStr;
use serde::{Serialize, Deserialize};
use crate::drain::drain_config::DrainConfig;

#[derive(Debug, PartialEq)]
pub enum TemplateType {
    NEW,
    /// 仅 content 发生合并/演化，label 未变。
    ContentUpdate,
    /// 仅 label 由非 normal 提升为 normal（精确匹配 + normal 提升），content 未变。
    LabelUpdate,
    /// content 合并的同时 label 也由非 normal 提升为 normal。
    BothUpdate,
    EXIST
}

#[derive(Debug, PartialEq, Serialize, Deserialize, Clone)]
pub enum LabelSource {
    SYSTEM,
    CUSTOM,
    BUILTIN
}

impl FromStr for LabelSource {
    type Err = String;

    fn from_str(s: &str) -> Result<Self, Self::Err> {
        match s {
            "SYSTEM" => Ok(LabelSource::SYSTEM),
            "CUSTOM" => Ok(LabelSource::CUSTOM),
            "BUILTIN" => Ok(LabelSource::BUILTIN),
            _ => Err(format!("unknown enum value: {}", s)),
        }
    }
}

impl LabelSource {
    pub fn as_str(&self) -> &str {
        match self {
            LabelSource::SYSTEM => "SYSTEM",
            LabelSource::CUSTOM => "CUSTOM",
            LabelSource::BUILTIN => "BUILTIN",
        }
    }
}

impl TemplateType {
    pub fn is_new(&self) -> bool {
        match self {
            TemplateType::NEW => true,
            TemplateType::ContentUpdate => false,
            TemplateType::LabelUpdate => false,
            TemplateType::BothUpdate => false,
            TemplateType::EXIST => false
        }
    }

    /// 是否为任意形式的更新（content / label / 二者同时）。
    pub fn is_update(&self) -> bool {
        match self {
            TemplateType::NEW => false,
            TemplateType::ContentUpdate => true,
            TemplateType::LabelUpdate => true,
            TemplateType::BothUpdate => true,
            TemplateType::EXIST => false
        }
    }

    pub fn is_content_update(&self) -> bool {
        matches!(self, TemplateType::ContentUpdate | TemplateType::BothUpdate)
    }

    pub fn is_label_update(&self) -> bool {
        matches!(self, TemplateType::LabelUpdate | TemplateType::BothUpdate)
    }

    /// content 与 label 同时更新。
    pub fn is_both_update(&self) -> bool {
        matches!(self, TemplateType::BothUpdate)
    }

    pub fn is_exist(&self) -> bool {
        match self {
            TemplateType::NEW => false,
            TemplateType::ContentUpdate => false,
            TemplateType::LabelUpdate => false,
            TemplateType::BothUpdate => false,
            TemplateType::EXIST => true
        }
    }
}

#[derive(Debug, Serialize, Deserialize)]
struct Node {
    children: HashMap<String, Node>,
    template_ids: Option<HashSet<u64>>
}

impl Node {
    fn new() -> Node {
        Node { children: HashMap::new(), template_ids: None }
    }

    fn match_or_add_template(
        &mut self,
        wildcard: &str,
        tokens: &Vec<String>,
        id_generator: &Box<dyn IdGenerator>,
        template_cache: &mut HashMap<u64, DrainTemplate>,
        sim_th: f64,
        max_wildcard_rate: f64,
        normal:bool,
        source: LabelSource
    ) -> (DrainTemplate, TemplateType) {
        match self.template_ids {
            Some(ref mut ids) => {
                let mut templates = Vec::new();
                for template_id in ids.iter() {
                    if let Some(template) = template_cache.get(template_id) {
                        templates.push(template);
                    }
                }
                let (max_sim, matched) = find_most_similar(tokens, &templates, wildcard);
                if max_sim >= 1.0 {
                    // 返回已有模板，但是更新模板的 normal 属性
                    let exist = template_cache.get_mut(&matched).unwrap();
                    if normal && !exist.normal {
                        exist.normal = true;
                        (exist.clone(), TemplateType::LabelUpdate)
                    } else {
                        (exist.clone(), TemplateType::EXIST)
                    }
                } else if max_sim >= sim_th {
                    let old = template_cache.get_mut(&matched).unwrap();
                    let merged_content = merge_template(old.content(), tokens, wildcard);
                    // 如果合并后通配符占比超过阈值则不应该合并，而是需要新增一个模板
                    if wildcard_rate(&merged_content, wildcard) > max_wildcard_rate {
                        let template_id = id_generator.next_number(tokens);
                        let new_template = DrainTemplate::new(template_id, tokens.clone(), normal, source);
                        self.template_ids.get_or_insert(HashSet::new()).insert(template_id);
                        return (template_cache.entry(template_id).or_insert(new_template).clone(), TemplateType::NEW)
                    }
                    old.set_content(merged_content);
                    // 合并 content 的同时，如果 normal=true 且原模板非 normal，
                    // 则 label 也被提升 —— 二者同时更新。
                    let label_changed = normal && !old.normal;
                    if normal {
                        old.normal = normal;
                    }
                    let template_type = if label_changed {
                        TemplateType::BothUpdate
                    } else {
                        TemplateType::ContentUpdate
                    };
                    (old.clone(), template_type)
                } else {
                    let template_id = id_generator.next_number(tokens);
                    let new_template = DrainTemplate::new(template_id, tokens.clone(), normal, source);
                    self.template_ids.get_or_insert(HashSet::new()).insert(template_id);
                    (template_cache.entry(template_id).or_insert(new_template).clone(), TemplateType::NEW)
                }
            },
            None => {
                let mut ids = HashSet::new();
                let id = id_generator.next_number(tokens);
                ids.insert(id);
                self.template_ids = Some(ids);
                (template_cache.entry(id).or_insert(DrainTemplate::new(id, tokens.clone(), normal, source)).clone(), TemplateType::NEW)
            }
        }
    }

    fn import_one_template(
        &mut self,
        wildcard: &str,
        template: DrainTemplate,
        template_cache: &mut HashMap<u64, DrainTemplate>,
        sim_th: f64,
        max_wildcard_rate: f64
    ) -> (DrainTemplate, TemplateType) {
        match self.template_ids {
            Some(ref mut ids) => {
                let mut templates = Vec::new();
                for template_id in ids.iter() {
                    if let Some(template) = template_cache.get(template_id) {
                        templates.push(template);
                    }
                }
                let (max_sim, matched) = find_most_similar(template.content(), &templates, wildcard);
                if max_sim >= 1.0 {
                    (template_cache.get(&matched).unwrap().clone(), TemplateType::EXIST)
                } else if max_sim >= sim_th {
                    let old = template_cache.get_mut(&matched).unwrap();
                    let merged_content = merge_template(old.content(), template.content(), wildcard);
                    if wildcard_rate(&merged_content, wildcard) > max_wildcard_rate {
                        // 合并之后 <*> 超过阈值，则按照新模板处理
                        self.template_ids.get_or_insert(HashSet::new()).insert(template.id());
                        return (template_cache.entry(template.id()).or_insert(template).clone(), TemplateType::NEW);
                    }
                    old.set_content(merge_template(old.content(), template.content(), wildcard));
                    (old.clone(), TemplateType::ContentUpdate)
                } else {
                    self.template_ids.get_or_insert(HashSet::new()).insert(template.id());
                    (template_cache.entry(template.id()).or_insert(template).clone(), TemplateType::NEW)
                }
            },
            None => {
                let mut ids = HashSet::new();
                ids.insert(template.id());
                self.template_ids = Some(ids);
                (template_cache.entry(template.id()).or_insert(template).clone(), TemplateType::NEW)
            }
        }
    }
}

pub struct DefaultDrain {
    module: String,
    scope: String,
    depth: usize,
    max_children: usize,
    wildcard: String,
    root: HashMap<u64, Node>,
    id_generator: Box<dyn IdGenerator>,
    template_cache: HashMap<u64, DrainTemplate>,
    sim_th: f64,
    max_wildcard_rate: f64,
    capacity: usize
}

impl DefaultDrain {
    pub fn new(id_generator: Box<dyn IdGenerator>, module: String, scope: String, config: &DrainConfig) -> DefaultDrain {
        DefaultDrain {
            depth: config.depth,
            max_children: config.max_children,
            wildcard: config.wildcard.clone(),
            root: HashMap::new(),
            id_generator,
            sim_th:config.sim_th,
            template_cache: HashMap::new(),
            module,
            scope,
            max_wildcard_rate: config.max_wildcard_rate,
            capacity: config.max_capacity
        }
    }

    pub fn display(&self) {
        println!("{:?}", self.root);
        println!("{:?}:?", self.template_cache);
    }

    pub fn get_module(&self) -> &str {
        self.module.as_str()
    }

    fn tree_search<'tree>(
        root: &'tree mut HashMap<u64, Node>, tokens: &Vec<String>,
        max_child: usize, depth: usize, wildcard: &str)
        -> &'tree mut Node {
        let mut node = root.entry(tokens.len() as u64).or_insert(Node::new());
        for (index, token) in tokens.iter().enumerate() {
            if (index + 1) >= depth {
                break;
            }
            node = Self::next_child(node, max_child, token, wildcard);
        }
        node
    }

    fn next_child<'a>(node: &'a mut Node, max_children: usize, child_path: &str, wildcard: &str) -> &'a mut Node {
        if node.children.contains_key(child_path) {
            return node.children.get_mut(child_path).unwrap();
        }
        if node.children.len() + 1 < max_children {
            return node.children.entry(String::from(child_path)).or_insert(Node::new())
        }

        if node.children.contains_key(wildcard) {
            return node.children.get_mut(wildcard).unwrap();
        }

        node.children.entry(String::from(wildcard)).or_insert(Node::new())
    }
}

impl DrainTree for DefaultDrain {
    fn search_or_insert(&mut self, tokens: &Vec<String>, normal: bool, source: LabelSource) -> (TemplateType, DrainTemplate)
    {
        // 查找存储模板的目标节点
        let mut depth = self.depth;
        if tokens.len() > 1 {
            depth = min(self.depth, tokens.len());
        }
        let node = Self::tree_search(&mut self.root, tokens, self.max_children, depth, self.wildcard.as_str());
        let sim_th = sim_th(tokens.len());
        // 保存模板到节点中
        let (template, template_type) = node.match_or_add_template(
            self.wildcard.as_str(),
            tokens,
            &self.id_generator,
            &mut self.template_cache,
            sim_th, self.max_wildcard_rate,
            normal,
            source
        );
        (template_type, template.clone())
    }

    fn import(&mut self, to_import: DrainTemplate) -> (TemplateType, DrainTemplate) {
        let depth = min(self.depth, to_import.content().len() - 1);
        let node = Self::tree_search(&mut self.root, to_import.content(), self.max_children, depth, self.wildcard.as_str());
        let sim_th = sim_th(to_import.content().len());
        // 保存模板到节点中
        let (template, template_type) = node.import_one_template(
            self.wildcard.as_str(),
            to_import,
            &mut self.template_cache,
            sim_th,
            self.max_wildcard_rate
        );
        (template_type, template.clone())
    }

    fn get_templates(&self, template_ids: Vec<u64>) -> HashMap<u64, String> {
        let mut result = HashMap::new();
        if self.template_cache.is_empty() {
            return result;
        }
        for template_id in template_ids {
            if let Some(template) = self.template_cache.get(&template_id) {
                result.insert(template_id, template.content().join(" "));
            }
        }
        result
    }

    fn remove_over_limit(&mut self) {
        info!("{}-{} capacity is {}, size is {}, start remove over limit.",
            &self.scope, &self.module, self.capacity, self.size());
        let remove_count = self.capacity / 10 * 3;
        let old_template = std::mem::take(&mut self.template_cache);
        self.clear();
        let mut removed = 0;
        for (_, template) in old_template.into_iter() {
            // 白名单或者 custom 的都保留, 如果删除数量达标了也都保留
            if removed >= remove_count || template.normal || template.label_source != LabelSource::SYSTEM {
                self.import(template);
                continue;
            }
            removed = removed + 1;
        }
        info!("{}-{} {} templates left after remove.", &self.scope, &self.module, self.size());
    }

    fn clear(&mut self) {
        self.template_cache.clear();
        self.root.clear();
    }

    fn size(&self) -> usize {
        self.template_cache.len()
    }

    fn capacity(&self) -> usize {
        self.capacity
    }

    fn set_capacity(&mut self, capacity: usize) {
        self.capacity = capacity;
    }
}

pub fn seq_distance(template_content: &Vec<String>, compared: &Vec<String>, wildcard: &str) -> (f64, usize) {
    if template_content.len() == 0 {
        return (1.0, 0);
    }
    let total: usize = template_content.len();
    let mut same: usize = 0;
    let mut wildcard_count: usize = 0;
    for (token1, token2) in template_content.iter().zip(compared.iter()) {
        if token1 == wildcard {
            wildcard_count += 1;
            continue;
        }
        if token1 == token2 {
            // 如果不包含字符或者数字，那么这个字符不计算相似度
            same += 1;
        }
    }
    same += wildcard_count;
    let sim = match total == 0 {
        true => 1.0,
        false => same as f64 / total as f64,
    };
    (sim, wildcard_count)
}

// float比较
pub fn compare_float(float1: f64, float2: f64) -> bool {
    let diff = (float1 - float2).abs();
    diff <= 1e-10
}

pub fn merge_template(exist: &Vec<String>, tokens: &Vec<String>, wildcard: &str) -> Vec<String> {
    let mut template_content = Vec::new();
    for (token1, token2) in exist.iter().zip(tokens.iter()) {
        template_content.push(
            match token1 == token2 {
                true => String::from(token1),
                false => wildcard.to_string(),
            });
    }
    template_content
}

fn find_most_similar(tokens: &Vec<String>, templates: &Vec<&DrainTemplate>, wildcard: &str) -> (f64, u64) {
    let mut max_sim = -1.0;
    let mut min_wildcard = 0;
    let mut matched = 0;
    // 搜索相似度最高的模板
    for template in templates.iter() {
        let (curr_similar, curr_wildcard) = seq_distance(template.content(), tokens, wildcard);
        if curr_similar > max_sim || compare_float(curr_similar, max_sim) && curr_wildcard < min_wildcard {
            max_sim = curr_similar;
            matched = template.id();
            min_wildcard = curr_wildcard;
        }
    }
    (max_sim, matched)
}

/**
如果模板继续进化，那么模板最终的
**/
pub fn wildcard_rate(template: &Vec<String>, wildcard: &str) -> f64 {
    let mut wildcard_count: usize = 0;
    for token in template {
        if token == wildcard {
            wildcard_count += 1;
        }
    }
    let next_rate: f64 = wildcard_count as f64/ template.len() as f64;
    next_rate
}

pub fn sim_th(token_length: usize) -> f64 {
    if token_length == 1 {
        1.0
    } else if token_length == 2 {
        0.5
    } else if token_length == 3 {
        0.60
    } else if token_length == 4 {
        0.75
    } else if token_length == 5 {
        0.6
    } else if token_length == 6 {
        0.60
    } else if token_length == 7 {
        0.57
    } else if token_length == 8 {
        0.6
    } else if token_length == 9 {
        0.6
    } else {
        0.6
    }
}