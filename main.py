import json 
from typing import List, Dict, Any, Optional 

from astrbot.api import logger, AstrBotConfig 
from astrbot.api.event import filter, AstrMessageEvent, MessageChain 
from astrbot.api.star import Context, Star, register 
import astrbot.api.message_components as Comp 

# 检查是否为 aiocqhttp 平台，因为合并转发是其特性 
try: 
    from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent 
    IS_AIOCQHTTP = True 
except ImportError: 
    IS_AIOCQHTTP = False 


@register("forward_reader", "EraAsh", "一个使用 LLM 分析合并转发消息内容的插件", "1.1.1", "https://github.com/EraAsh/astrbot_plugin_forward_reader") 
class ForwardReader(Star): 
    def __init__(self, context: Context, config: AstrBotConfig): 
        super().__init__(context) 
        self.config = config
        self._load_config() 

    def _load_config(self): 
        """加载配置。根据用户要求，默认关闭所有自动触发。""" 
        self.enable_direct_analysis = self.config.get("enable_direct_analysis", False) 
        self.enable_reply_analysis = self.config.get("enable_reply_analysis", False) 

    @filter.event_message_type(filter.EventMessageType.ALL) 
    async def on_any_message(self, event: AstrMessageEvent, *args, **kwargs): 
        """ 
        监听所有消息，在满足“回复转发”或“直接发送转发”且“唤醒机器人”的条件下进行分析。 
        """ 
        if not IS_AIOCQHTTP or not isinstance(event, AiocqhttpMessageEvent): 
            return 

        forward_id: Optional[str] = None 
        reply_seg: Optional[Comp.Reply] = None 
        user_query: str = event.message_str.strip() 

        is_bot_awaken = event.is_at_or_wake_command
        
        is_default_wake_trigger = not self.enable_direct_analysis and not self.enable_reply_analysis and is_bot_awaken

        if not self.enable_direct_analysis and not self.enable_reply_analysis and not is_bot_awaken:
             return
        
        # 遍历消息链寻找合并转发或对合并转发的引用 
        for seg in event.message_obj.message: 
            if isinstance(seg, Comp.Forward): 
                # 场景 1: 直接发送合并转发
                if self.enable_direct_analysis or is_bot_awaken:
                    forward_id = seg.id 
                    if not user_query:
                        user_query = "请总结一下这个聊天记录" 
                    if forward_id: 
                        break
            elif isinstance(seg, Comp.Reply): 
                reply_seg = seg 

        # 场景 2: 如果是回复消息，则检查被回复的是否是合并转发 
        if (self.enable_reply_analysis or is_bot_awaken) and not forward_id and reply_seg: 
            try: 
                client = event.bot 
                original_msg = await client.api.call_action('get_msg', message_id=reply_seg.id) 
                
                if original_msg and 'message' in original_msg: 
                    original_message_chain = original_msg['message'] 
                    if isinstance(original_message_chain, list): 
                        for segment in original_message_chain: 
                            if isinstance(segment, dict) and segment.get("type") == "forward": 
                                forward_id = segment.get("data", {}).get("id") 
                                
                                if forward_id:
                                    if not user_query: 
                                        user_query = "请总结一下这个聊天记录" 
                                    break 

            except Exception as e: 
                logger.warning(f"获取被回复消息详情失败: {e}") 

        # 最终触发：找到转发ID AND 用户有明确的提问
        if forward_id and user_query:
            try: 
                if not is_default_wake_trigger:
                    await event.send(event.chain_result([Comp.Reply(id=event.message_obj.message_id), Comp.Plain("正在分析聊天记录，请稍候...")])) 

                # 1. 提取合并转发内容 
                extracted_texts, image_urls = await self._extract_forward_content(event, forward_id) 
                if not extracted_texts and not image_urls: 
                    yield event.plain_result("无法从合并转发消息中提取到任何有效内容。") 
                    return 

                # 2. 构建用于LLM分析的最终提示词 
                chat_records = "\n".join(extracted_texts) 
                final_prompt = ( 
                    f"这是用户的问题：'{user_query}'\n\n" 
                    f"请根据以下聊天记录内容来回答用户的问题。聊天记录如下：\n" 
                    f"--- 聊天记录开始 ---\n" 
                    f"{chat_records}\n" 
                    f"--- 聊天记录结束 ---" 
                ) 

                # 保留 info 日志输出
                logger.info(f"ForwardReader: 准备向LLM发送请求，Prompt长度: {len(final_prompt)}, 图片数量: {len(image_urls)}") 

                # 3. [核心] 使用 event.request_llm() 发起请求 
                yield event.request_llm( 
                    prompt=final_prompt, 
                    image_urls=image_urls 
                ) 

                # 4. 阻止事件继续传播，防止其他插件响应 
                event.stop_event() 

            except Exception as e: 
                logger.error(f"分析转发消息失败: {e}") 
                yield event.plain_result(f"分析失败: {e}") 
        
    async def _extract_forward_content(self, event: AiocqhttpMessageEvent, forward_id: str) -> tuple[list[str], list[str]]:  
        """ 
        从合并转发消息中提取文本和图片URL。 
        """ 
        client = event.bot 
        try: 
            forward_data = await client.api.call_action('get_forward_msg', id=forward_id) 
        except Exception as e: 
            logger.error(f"调用 get_forward_msg API 失败: {e}") 
            raise ValueError("获取合并转发内容失败，可能是消息已过期或API问题。") 

        if not forward_data or "messages" not in forward_data: 
            raise ValueError("获取到的合并转发内容为空。") 

        extracted_texts = [] 
        image_urls = [] 

        for message_node in forward_data["messages"]: 
            sender_name = message_node.get("sender", {}).get("nickname", "未知用户") 
            raw_content = message_node.get("message") or message_node.get("content", []) 

            content_chain = [] 
            if isinstance(raw_content, str): 
                try: 
                    parsed_content = json.loads(raw_content) 
                    if isinstance(parsed_content, list): 
                        content_chain = parsed_content 
                    else: 
                        logger.debug(f"从字符串解析的内容不是列表: {parsed_content}") 
                except (json.JSONDecodeError, TypeError): 
                    logger.debug(f"无法将内容字符串解析为JSON，当作纯文本处理: {raw_content}") 
                    content_chain = [{"type": "text", "data": {"text": raw_content}}] 
            elif isinstance(raw_content, list): 
                content_chain = raw_content 

            node_text_parts = [] 
            if isinstance(content_chain, list): 
                for segment in content_chain: 
                    if isinstance(segment, dict): 
                        seg_type = segment.get("type") 
                        seg_data = segment.get("data", {}) 
                        if seg_type == "text": 
                            text = seg_data.get("text", "") 
                            if text: 
                                node_text_parts.append(text) 
                        elif seg_type == "image": 
                            url = seg_data.get("url") 
                            if url: 
                                image_urls.append(url) 
                                node_text_parts.append("[图片]") 
            
            full_node_text = "".join(node_text_parts).strip() 
            if full_node_text: 
                extracted_texts.append(f"{sender_name}: {full_node_text}") 

        return extracted_texts, image_urls 
    
    async def terminate(self): 
        pass