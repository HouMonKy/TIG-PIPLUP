use std::collections::HashMap;
use crate::drain::default_drain::{LabelSource, TemplateType};
use crate::drain::drain_template::DrainTemplate;

/**
    drain树实现
**/
pub trait DrainTree where Self: Send + Sync {
    /**
        给定一个经过预处理的日志的token列表，返回一个已存在或者新增的模板
        @param  tokens 经过预处理将一些可能的变量转换成通配符的tokens列表
        @return &template 表示tokens匹配到的模板信息
        @return TemplateType 五种场景：NEW 新模板、ContentUpdate 内容合并、LabelUpdate 仅 label 提升、BothUpdate content+label 同时更新、EXIST 老模板
    **/
    fn search_or_insert(&mut self, tokens: &Vec<String>, normal: bool, source: LabelSource) -> (TemplateType, DrainTemplate);

    /**
        通过向drain提供一个已经存在template，将这个存在的template加载到drain树上去，如果已经存在等价的模板则使用等价模板，如果不存在则将导入当前模板
        注意
        @param template 已经存在的老模板
        @return &template 表示tokens匹配到的模板信息
        @return TemplateType 五种场景：NEW 新模板、ContentUpdate 内容合并、LabelUpdate 仅 label 提升、BothUpdate content+label 同时更新、EXIST 老模板
    **/
    fn import(&mut self, template: DrainTemplate) -> (TemplateType, DrainTemplate);

    /**
        判断给定的id是否存在对应的模板内容
        @param template_ids 待查询的模板
        @return template_map 查询到的模板内容
    **/
    fn get_templates(&self, template_ids: Vec<u64>) -> HashMap<u64, String>;

    /// 删除超过 capacity 的模板，如果超过上限，则只保留 CUSTOM BUILTIN 和 normal 的模板
    fn remove_over_limit(&mut self);

    fn clear(&mut self);

    fn size(&self) -> usize;

    fn capacity(&self) -> usize;

    fn set_capacity(&mut self, capacity: usize);
}
