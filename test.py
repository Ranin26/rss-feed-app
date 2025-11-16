import flet as ft

def main(page: ft.Page):
    page.padding = 20
    page.bgcolor = ft.Colors.GREY_900
    
    # OutlinedButton with border (default)
    with_border = ft.OutlinedButton(
        text="With Border (Default)",
        style=ft.ButtonStyle(
            side=ft.BorderSide(width=2, color=ft.Colors.BLUE),
        )
    )
    
    # OutlinedButton with no border (width=0)
    no_border_outlined = ft.OutlinedButton(
        text="No Border (width=0)",
        style=ft.ButtonStyle(
            side=ft.BorderSide(width=0),
        )
    )
    
    # OutlinedButton with transparent border
    transparent = ft.OutlinedButton(
        text="Transparent Border",
        style=ft.ButtonStyle(
            side=ft.BorderSide(width=2, color=ft.Colors.TRANSPARENT),
        )
    )
    
    # TextButton (no border by default)
    text_btn = ft.TextButton(
        text="TextButton (No Border)",
        style=ft.ButtonStyle(
            top=ft.BorderSide(width=2, color=ft.Colors.TRANSPARENT),
        )
    )
    
    # ElevatedButton (no border)
    elevated = ft.ElevatedButton(
        text="ElevatedButton (No Border)",
        style=ft.ButtonStyle(
            bgcolor=ft.Colors.BLUE_700,
        )
    )
    
    # FilledButton (no border)
    filled = ft.FilledButton(
        text="FilledButton (No Border)",
    )
    
    page.add(
        ft.Text("With Border:", size=16, weight=ft.FontWeight.BOLD),
        with_border,
        ft.Divider(height=20),
        
        ft.Text("No Border Methods:", size=16, weight=ft.FontWeight.BOLD),
        no_border_outlined,
        transparent,
        text_btn,
        elevated,
        filled,
    )

ft.app(target=main)