"""Streamlit 聊天入口，负责会话管理和页面展示。"""

import streamlit as st

from .. import __version__
from ..bootstrap import build_service
from ..core.errors import QAError
from ..models.conversation import ConversationState


def show_details(details: dict) -> None:
    for notice in details.get("notices", []):
        st.caption(notice)
    sources = details.get("sources", [])
    if sources:
        with st.expander("查看来源"):
            for source in sources:
                label = f"[{source['source_id']}] {source['title']}"
                url = source.get("url")
                if url and url.startswith(("https://", "http://")):
                    st.markdown(f"[{label}]({url})")
                else:
                    st.write(label)
                st.caption("资料形式：" + source["evidence_form"])


def main() -> None:
    st.set_page_config(page_title="宁夏葡萄酒智能问答", page_icon="🍇")
    st.title("宁夏葡萄酒智能问答")
    st.caption("查询酒庄和产品资料、筛选产品、了解葡萄酒知识。")
    try:
        if "qa_service" not in st.session_state or st.session_state.get("qa_service_revision") != __version__:
            if "qa_service" in st.session_state:
                st.session_state.qa_service.close()
            st.session_state.qa_service = build_service()
            st.session_state.qa_service_revision = __version__
        if "qa_state" not in st.session_state:
            st.session_state.qa_state = ConversationState()
            st.session_state.qa_details = {}
    except QAError as exc:
        st.error(str(exc))
        st.stop()
    service, state = st.session_state.qa_service, st.session_state.qa_state
    with st.sidebar:
        if service.settings.backend == "demo":
            st.info("当前为离线演示，读取已收录资料；知识语义问答尚未开启。")
        if st.button("开启新会话", use_container_width=True):
            state.reset()
            st.session_state.qa_details = {}
            st.rerun()
        st.write("可以这样问：")
        st.caption("介绍一下西鸽酒庄\n\n找几款 750 毫升的干红\n\n要含赤霞珠的\n\n300元以下的葡萄酒\n\n第二款多少钱？")
    for index, message in enumerate(state.messages):
        with st.chat_message(message["role"]):
            st.markdown(message["content"])
            show_details(st.session_state.qa_details.get(index, {}))
    question = st.chat_input("请输入问题", max_chars=2000)
    if question:
        with st.chat_message("user"):
            st.markdown(question)
        with st.chat_message("assistant"):
            with st.spinner("正在查询资料…"):
                response = service.ask(question, state)
            st.markdown(response.text)
            details = response.model_dump(mode="json", include={"sources", "notices"})
            st.session_state.qa_details[len(state.messages) - 1] = details
            show_details(details)
